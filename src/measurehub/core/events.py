"""Real-time events (technical specification, 7.2): message builders and Redis channels.

Every message is a JSON object with a ``type`` and a ``ts`` (UTC, ISO 8601). Progress and segment
data are absolute (``points_done``, ``from_idx``), so a client may receive a message twice or
miss one and still end up with the right picture.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

GLOBAL_CHANNEL = "events:global"

Event = dict[str, Any]


def job_channel(job_id: uuid.UUID | str) -> str:
    return f"events:job:{job_id}"


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def status_event(
    job_id: uuid.UUID | str, status: str, instrument_id: uuid.UUID | str | None = None
) -> Event:
    event: Event = {"type": "status", "job_id": str(job_id), "status": status, "ts": timestamp()}
    if instrument_id is not None:
        event["instrument_id"] = str(instrument_id)
    return event


def progress_event(
    job_id: uuid.UUID | str,
    points_done: int,
    points_total: int,
    segments_done: int,
    segments_total: int,
) -> Event:
    return {
        "type": "progress",
        "job_id": str(job_id),
        "points_done": points_done,
        "points_total": points_total,
        "segments_done": segments_done,
        "segments_total": segments_total,
        "ts": timestamp(),
    }


def segment_event(
    job_id: uuid.UUID | str,
    from_idx: int,
    freq_hz: Sequence[float],
    s11_db: Sequence[float],
    s21_db: Sequence[float],
) -> Event:
    return {
        "type": "segment",
        "job_id": str(job_id),
        "from_idx": from_idx,
        "freq_hz": list(freq_hz),
        "s11_db": list(s11_db),
        "s21_db": list(s21_db),
        "ts": timestamp(),
    }


def log_event(job_id: uuid.UUID | str, level: str, message: str) -> Event:
    return {
        "type": "log",
        "job_id": str(job_id),
        "level": level,
        "message": message,
        "ts": timestamp(),
    }


def error_event(job_id: uuid.UUID | str, code: str, message: str) -> Event:
    return {
        "type": "error",
        "job_id": str(job_id),
        "code": code,
        "message": message,
        "ts": timestamp(),
    }


def instrument_event(instrument_id: uuid.UUID | str, status: str) -> Event:
    return {
        "type": "instrument",
        "instrument_id": str(instrument_id),
        "status": status,
        "ts": timestamp(),
    }


class EventSink(Protocol):
    """Where the worker sends events. Publishing must never break a measurement."""

    def job(self, job_id: uuid.UUID, event: Event, *, also_global: bool = False) -> None: ...

    def instrument(self, instrument_id: uuid.UUID, status: str) -> None: ...


class NullEventSink:
    def job(self, job_id: uuid.UUID, event: Event, *, also_global: bool = False) -> None:
        pass

    def instrument(self, instrument_id: uuid.UUID, status: str) -> None:
        pass
