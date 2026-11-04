"""Atomic job status transitions for synchronous code (worker, watchdog)."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.orm import Session

from measurehub.core.events import EventSink, status_event
from measurehub.core.states import JobStatus, can_transition
from measurehub.db.models import Job
from measurehub.worker.events import record_event


def transition(
    session: Session,
    job_id: uuid.UUID,
    old: JobStatus,
    new: JobStatus,
    *,
    events: EventSink | None = None,
    **values: Any,
) -> bool:
    """Atomic UPDATE ... WHERE status = old; False if the job was not in ``old``.

    A successful change is written to the job log and published as a ``status`` event.
    """
    assert can_transition(old, new), f"{old} -> {new} is not allowed"
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job).where(Job.id == job_id, Job.status == old).values(status=new, **values)
        ),
    )
    session.commit()
    if result.rowcount != 1:
        return False
    announce(session, events, job_id, old, new)
    return True


def announce(
    session: Session, events: EventSink | None, job_id: uuid.UUID, old: JobStatus, new: JobStatus
) -> None:
    level = "error" if new is JobStatus.FAILED else "info"
    record_event(session, job_id, level, "status", {"status": new.value, "from": old.value})
    if events is not None:
        events.job(job_id, status_event(job_id, new.value), also_global=True)
