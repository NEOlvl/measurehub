"""Job lifecycle (state diagram of the technical specification, section 5.4)."""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PROCESSING = "processing"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # queued -> failed is not in the diagram: it is the outcome of a failed enqueue
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.PROCESSING, JobStatus.CANCELLING, JobStatus.QUEUED, JobStatus.FAILED}
    ),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED}),
    JobStatus.PROCESSING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset({JobStatus.PROCESSING}),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

UNFINISHED = frozenset(
    {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PROCESSING, JobStatus.CANCELLING}
)


def can_transition(current: JobStatus, new: JobStatus) -> bool:
    return new in ALLOWED_TRANSITIONS[current]
