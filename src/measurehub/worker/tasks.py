"""Celery tasks."""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

from celery import Task
from redis import Redis

from measurehub.core.logging import configure_logging
from measurehub.core.settings import get_settings
from measurehub.db.session import make_sync_engine, make_sync_sessionmaker
from measurehub.drivers.registry import create_driver
from measurehub.services.instruments import poll_instruments
from measurehub.worker.app import celery_app
from measurehub.worker.engine import EngineContext, InstrumentBusyError, execute_job
from measurehub.worker.watchdog import requeue_stale_jobs

BUSY_RETRY_S = 5


@lru_cache
def get_context() -> EngineContext:
    """Per-process resources, created on first use (i.e. after the worker process forked)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    return EngineContext(
        session_factory=make_sync_sessionmaker(make_sync_engine(settings)),
        redis=Redis.from_url(settings.redis_url),
        driver_factory=lambda key, resource: create_driver(
            key, resource, timeout_ms=settings.visa_timeout_ms
        ),
        lock_ttl_s=settings.instrument_lock_ttl_s,
    )


@celery_app.task(bind=True, name="measurehub.run_job")
def run_job(self: Task[Any, Any], job_id: str) -> str | None:
    """Measure a job. If the instrument is busy, retry later; the job stays queued."""
    try:
        status = execute_job(uuid.UUID(job_id), get_context())
    except InstrumentBusyError:
        raise self.retry(countdown=BUSY_RETRY_S, max_retries=None) from None
    return status.value if status else None


@celery_app.task(name="measurehub.watchdog")
def watchdog() -> dict[str, int]:
    """Re-queue jobs whose worker died (runs from Celery beat)."""
    settings = get_settings()
    report = requeue_stale_jobs(
        get_context().session_factory,
        lambda job_id: run_job.delay(str(job_id)),
        stale_after_s=settings.watchdog_stale_s,
        max_requeues=settings.watchdog_max_requeues,
    )
    return {
        "requeued": len(report.requeued),
        "failed": len(report.failed),
        "cancelled": len(report.cancelled),
    }


@celery_app.task(name="measurehub.poll_instruments")
def poll_instruments_task() -> dict[str, str]:
    """Refresh the status of idle instruments (runs from Celery beat)."""
    ctx = get_context()
    statuses = poll_instruments(ctx.session_factory, ctx.redis, ctx.driver_factory)
    return {str(instrument_id): status.value for instrument_id, status in statuses.items()}
