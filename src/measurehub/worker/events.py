"""Event publishing from the worker: Redis pub/sub plus the persistent job log."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from measurehub.core.events import GLOBAL_CHANNEL, Event, instrument_event, job_channel
from measurehub.db.models import JobEvent

log = structlog.get_logger()


class RedisEventSink:
    """Publishes to ``events:job:{id}`` (and ``events:global`` for status changes).

    Errors are logged and swallowed: a Redis hiccup must not fail the measurement.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    def job(self, job_id: uuid.UUID, event: Event, *, also_global: bool = False) -> None:
        payload = json.dumps(event)
        try:
            self._redis.publish(job_channel(job_id), payload)
            if also_global:
                self._redis.publish(GLOBAL_CHANNEL, payload)
        except Exception as exc:
            log.warning("event_publish_failed", error_type=type(exc).__name__, error=str(exc))

    def instrument(self, instrument_id: uuid.UUID, status: str) -> None:
        try:
            self._redis.publish(GLOBAL_CHANNEL, json.dumps(instrument_event(instrument_id, status)))
        except Exception as exc:
            log.warning("event_publish_failed", error_type=type(exc).__name__, error=str(exc))


def record_event(
    session: Session, job_id: uuid.UUID, level: str, event: str, payload: dict[str, Any]
) -> None:
    """Append a row to the persistent job log (``GET /jobs/{id}/events``)."""
    session.add(
        JobEvent(job_id=job_id, ts=datetime.now(UTC), level=level, event=event, payload=payload)
    )
    session.commit()
