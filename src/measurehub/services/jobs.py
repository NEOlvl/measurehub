"""Job creation and dispatch (API side)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import anyio.to_thread
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from measurehub.api.errors import ApiError, conflict, not_found, unprocessable
from measurehub.core.states import JobStatus, can_transition
from measurehub.db.models import Instrument, Job, Method, Sample, User
from measurehub.drivers.models import VnaSweepParams
from measurehub.drivers.segments import split_sweep
from measurehub.schemas.api import JobCreate
from measurehub.services.events import announce_status
from measurehub.services.methods import PROCESSORS_REQUIRING_SAMPLE, validate_params
from measurehub.worker.cancel import CANCEL_FLAG_TTL_S, cancel_key

Dispatcher = Callable[[uuid.UUID], str | None]
"""Hands a job to the background workers; returns the task id if there is one."""


async def create_job(
    session: AsyncSession,
    body: JobCreate,
    user: User,
    dispatcher: Dispatcher,
    redis: Any = None,
) -> Job:
    method = await session.get(Method, body.method_id)
    if method is None:
        raise not_found("Method", body.method_id)
    if method.is_archived:
        raise conflict("Method is archived")
    instrument = await session.get(Instrument, body.instrument_id)
    if instrument is None:
        raise not_found("Instrument", body.instrument_id)
    if not instrument.is_enabled:
        raise conflict("Instrument is disabled")
    if body.sample_id is not None and await session.get(Sample, body.sample_id) is None:
        raise not_found("Sample", body.sample_id)
    if body.sample_id is None and method.processor in PROCESSORS_REQUIRING_SAMPLE:
        raise unprocessable(f"Processor {method.processor!r} requires a sample")

    params = validate_params(method.measurement_type, {**method.params, **(body.params or {})})
    sweep = VnaSweepParams(**params)
    job = Job(
        method_id=method.id,
        instrument_id=instrument.id,
        sample_id=body.sample_id,
        params=params,
        status=JobStatus.QUEUED,
        points_total=sweep.points,
        segments_total=len(split_sweep(sweep)),
        created_by=user.id,
    )
    session.add(job)
    await session.commit()
    await announce_status(session, redis, job.id, None, JobStatus.QUEUED)
    await _dispatch(session, job, dispatcher, redis)
    return job


async def _dispatch(session: AsyncSession, job: Job, dispatcher: Dispatcher, redis: Any) -> None:
    try:
        job.celery_task_id = await anyio.to_thread.run_sync(dispatcher, job.id)
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error_code = "dispatch_failed"
        job.error_message = f"Could not enqueue the job: {type(exc).__name__}"
        job.finished_at = datetime.now(UTC)
        await session.commit()
        await announce_status(session, redis, job.id, JobStatus.QUEUED, JobStatus.FAILED)
        raise ApiError(503, "Service Unavailable", "Job queue is unavailable") from exc
    await session.commit()


async def retry_job(
    session: AsyncSession, job: Job, user: User, dispatcher: Dispatcher, redis: Any = None
) -> Job:
    """Create a new job with the same method, instrument, sample and parameters."""
    if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
        raise conflict(
            f"Only failed or cancelled jobs can be retried, this one is {job.status.value}"
        )
    body = JobCreate(
        method_id=job.method_id,
        instrument_id=job.instrument_id,
        sample_id=job.sample_id,
        params=job.params,
    )
    return await create_job(session, body, user, dispatcher, redis)


async def cancel_job(session: AsyncSession, redis: Any, job: Job) -> Job:
    """Cancel a job: at once if queued, after the current segment if running.

    Idempotent for a job that is already ``cancelling`` or ``cancelled``; a job that is
    processing or finished cannot be cancelled (409).
    """
    if job.status is JobStatus.QUEUED and await _move(
        session, redis, job, JobStatus.QUEUED, JobStatus.CANCELLED, finished_at=datetime.now(UTC)
    ):
        return job
    if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        # the flag first: the worker looks at it between segments
        await redis.set(cancel_key(job.id), "1", ex=CANCEL_FLAG_TTL_S)
        await _move(session, redis, job, JobStatus.RUNNING, JobStatus.CANCELLING)
    await session.refresh(job)
    if job.status not in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
        raise conflict(f"Job is {job.status.value} and cannot be cancelled")
    return job


async def _move(
    session: AsyncSession, redis: Any, job: Job, old: JobStatus, new: JobStatus, **values: Any
) -> bool:
    """Atomic ``UPDATE ... WHERE status = old``; True if this call changed the status."""
    assert can_transition(old, new)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Job).where(Job.id == job.id, Job.status == old).values(status=new, **values)
        ),
    )
    await session.commit()
    if result.rowcount == 1:
        await session.refresh(job)
        await announce_status(session, redis, job.id, old, new)
        return True
    return False
