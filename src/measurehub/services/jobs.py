"""Job creation and dispatch (API side)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import anyio.to_thread
from sqlalchemy.ext.asyncio import AsyncSession

from measurehub.api.errors import ApiError, conflict, not_found, unprocessable
from measurehub.core.states import JobStatus
from measurehub.db.models import Instrument, Job, Method, Sample, User
from measurehub.drivers.models import VnaSweepParams
from measurehub.drivers.segments import split_sweep
from measurehub.schemas.api import JobCreate
from measurehub.services.methods import PROCESSORS_REQUIRING_SAMPLE, validate_params

Dispatcher = Callable[[uuid.UUID], str | None]
"""Hands a job to the background workers; returns the task id if there is one."""


async def create_job(
    session: AsyncSession, body: JobCreate, user: User, dispatcher: Dispatcher
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
    await _dispatch(session, job, dispatcher)
    return job


async def _dispatch(session: AsyncSession, job: Job, dispatcher: Dispatcher) -> None:
    try:
        job.celery_task_id = await anyio.to_thread.run_sync(dispatcher, job.id)
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error_code = "dispatch_failed"
        job.error_message = f"Could not enqueue the job: {type(exc).__name__}"
        job.finished_at = datetime.now(UTC)
        await session.commit()
        raise ApiError(503, "Service Unavailable", "Job queue is unavailable") from exc
    await session.commit()
