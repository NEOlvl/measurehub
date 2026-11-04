"""Exclusive instrument lock in Redis (``lock:instrument:{id}``, TTL extended per segment)."""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from redis.exceptions import LockError


class LockLostError(Exception):
    """The lock expired or is owned by someone else: the instrument may be in use."""


class InstrumentLock:
    def __init__(self, redis: Any, instrument_id: uuid.UUID, ttl_s: int) -> None:
        self._ttl_s = ttl_s
        self._lock = redis.lock(
            f"lock:instrument:{instrument_id}", timeout=ttl_s, blocking=False, thread_local=False
        )

    def acquire(self) -> bool:
        return bool(self._lock.acquire(blocking=False))

    def extend(self) -> None:
        """Restart the TTL; raises :class:`LockLostError` if we no longer own the lock."""
        try:
            self._lock.extend(self._ttl_s, replace_ttl=True)
        except LockError as exc:
            raise LockLostError(str(exc)) from exc

    def release(self) -> None:
        with contextlib.suppress(LockError):
            self._lock.release()
