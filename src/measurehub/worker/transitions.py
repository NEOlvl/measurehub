"""Atomic job status transitions for synchronous code (worker, watchdog)."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.orm import Session

from measurehub.core.states import JobStatus, can_transition
from measurehub.db.models import Job


def transition(
    session: Session, job_id: uuid.UUID, old: JobStatus, new: JobStatus, **values: Any
) -> bool:
    """Atomic UPDATE ... WHERE status = old; False if the job was not in ``old``."""
    assert can_transition(old, new), f"{old} -> {new} is not allowed"
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job).where(Job.id == job_id, Job.status == old).values(status=new, **values)
        ),
    )
    session.commit()
    return result.rowcount == 1
