"""Cancel flag shared by the API (sets it) and the engine (checks it between segments)."""

from __future__ import annotations

import uuid

CANCEL_FLAG_TTL_S = 3600


def cancel_key(job_id: uuid.UUID) -> str:
    """Redis key of the cancel flag of a job."""
    return f"cancel:job:{job_id}"
