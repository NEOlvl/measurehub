"""Event helpers for the async API side (the worker has its own in ``worker/events.py``)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from measurehub.core.events import GLOBAL_CHANNEL, Event, job_channel, status_event
from measurehub.core.states import JobStatus
from measurehub.db.models import JobEvent

log = structlog.get_logger()


async def publish_job_event(
    redis: Any, job_id: uuid.UUID, event: Event, *, also_global: bool = False
) -> None:
    """Publish to the job channel (and the global one); failures are logged, not raised."""
    payload = json.dumps(event)
    try:
        await redis.publish(job_channel(job_id), payload)
        if also_global:
            await redis.publish(GLOBAL_CHANNEL, payload)
    except Exception as exc:
        log.warning("event_publish_failed", error_type=type(exc).__name__, error=str(exc))


async def announce_status(
    session: AsyncSession, redis: Any, job_id: uuid.UUID, old: JobStatus | None, new: JobStatus
) -> None:
    """Write a status change to the job log and publish it."""
    payload: dict[str, Any] = {"status": new.value}
    if old is not None:
        payload["from"] = old.value
    session.add(
        JobEvent(
            job_id=job_id,
            ts=datetime.now(UTC),
            level="error" if new is JobStatus.FAILED else "info",
            event="status",
            payload=payload,
        )
    )
    await session.commit()
    await publish_job_event(redis, job_id, status_event(job_id, new.value), also_global=True)
