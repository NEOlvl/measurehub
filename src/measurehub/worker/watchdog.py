"""Watchdog: finds jobs whose worker died (no heartbeat) and puts them back in the queue."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus
from measurehub.db.models import Job
from measurehub.worker.transitions import transition

log = structlog.get_logger()

Dispatcher = Callable[[uuid.UUID], object]


@dataclass(slots=True)
class WatchdogReport:
    requeued: list[uuid.UUID] = field(default_factory=list)
    failed: list[uuid.UUID] = field(default_factory=list)
    cancelled: list[uuid.UUID] = field(default_factory=list)


def requeue_stale_jobs(
    session_factory: sessionmaker[Session],
    dispatcher: Dispatcher,
    *,
    stale_after_s: float = 60,
    max_requeues: int = 2,
    now: datetime | None = None,
) -> WatchdogReport:
    """Handle running jobs without a heartbeat for ``stale_after_s`` seconds.

    - attempt < max_requeues: back to queued (and handed to the workers again);
    - otherwise failed with ``worker_lost``;
    - a stale cancelling job becomes cancelled: the worker that should stop it is gone.

    Every change is an UPDATE ... WHERE status = ... AND heartbeat_at < cutoff, so a job whose
    worker is still alive (fresh heartbeat) is never touched.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=stale_after_s)
    report = WatchdogReport()
    with session_factory() as session:
        stale = session.execute(
            select(Job.id, Job.status, Job.attempt).where(
                Job.status.in_([JobStatus.RUNNING, JobStatus.CANCELLING]),
                Job.heartbeat_at < cutoff,
            )
        ).all()
        for job_id, status, attempt in stale:
            if status is JobStatus.CANCELLING:
                if _stale_update(session, job_id, cutoff, status, JobStatus.CANCELLED):
                    report.cancelled.append(job_id)
            elif attempt < max_requeues:
                if _stale_update(
                    session, job_id, cutoff, status, JobStatus.QUEUED, attempt=Job.attempt + 1
                ):
                    report.requeued.append(job_id)
            elif _stale_update(
                session,
                job_id,
                cutoff,
                status,
                JobStatus.FAILED,
                error_code="worker_lost",
                error_message=f"Worker lost; the job was already re-queued {attempt} times",
                finished_at=datetime.now(UTC),
            ):
                report.failed.append(job_id)
        for job_id in list(report.requeued):
            _dispatch(session, dispatcher, job_id, report)
    log.info(
        "watchdog_checked",
        requeued=len(report.requeued),
        failed=len(report.failed),
        cancelled=len(report.cancelled),
    )
    return report


def _stale_update(
    session: Session,
    job_id: uuid.UUID,
    cutoff: datetime,
    old: JobStatus,
    new: JobStatus,
    **values: Any,
) -> bool:
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == old, Job.heartbeat_at < cutoff)
            .values(status=new, **values)
        ),
    )
    session.commit()
    if result.rowcount == 1:
        log.warning("watchdog_moved_job", job_id=str(job_id), old=old.value, new=new.value)
    return result.rowcount == 1


def _dispatch(
    session: Session, dispatcher: Dispatcher, job_id: uuid.UUID, report: WatchdogReport
) -> None:
    try:
        dispatcher(job_id)
    except Exception as exc:
        log.error("watchdog_dispatch_failed", job_id=str(job_id), exc_info=exc)
        if transition(
            session,
            job_id,
            JobStatus.QUEUED,
            JobStatus.FAILED,
            error_code="dispatch_failed",
            error_message=f"Could not re-enqueue the job: {type(exc).__name__}",
            finished_at=datetime.now(UTC),
        ):
            report.requeued.remove(job_id)
            report.failed.append(job_id)
