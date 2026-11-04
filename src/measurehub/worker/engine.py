"""Job engine: take the instrument, measure segment by segment, process, store.

Reliability features (technical specification, FR-12..FR-15, ADR-003):

- every segment is saved atomically and idempotently (``(job_id, idx)`` is unique);
- a failed segment is retried with exponential backoff (1, 2, 4 ... s, capped) after the link
  to the instrument was re-established;
- a re-queued job resumes from the first segment that is not completely saved;
- a cancel request (Redis flag or ``cancelling`` status) is honoured after the current segment.
"""

from __future__ import annotations

import contextlib
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus
from measurehub.db.models import (
    Instrument,
    InstrumentStatus,
    Job,
    MeasurementPoint,
    Method,
    Result,
    Sample,
)
from measurehub.drivers.base import InstrumentDriver
from measurehub.drivers.errors import (
    DriverError,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentTimeoutError,
)
from measurehub.drivers.models import Segment, SegmentData, VnaSweepParams
from measurehub.drivers.registry import DriverFactory
from measurehub.drivers.segments import split_sweep
from measurehub.processing import registry
from measurehub.processing.base import ProcessorInput, ProcessorResult
from measurehub.worker.cancel import cancel_key
from measurehub.worker.locks import InstrumentLock, LockLostError
from measurehub.worker.transitions import transition

log = structlog.get_logger()

MAX_BACKOFF_S = 30.0
RETRYABLE = (InstrumentConnectionError, InstrumentTimeoutError, InstrumentError)


class InstrumentBusyError(Exception):
    """Another job holds the instrument; the task should be retried later."""


@dataclass(frozen=True, slots=True)
class EngineContext:
    session_factory: sessionmaker[Session]
    redis: Any
    driver_factory: DriverFactory
    lock_ttl_s: int = 30
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True, slots=True)
class _JobSpec:
    """Everything the engine needs, copied out of the database up front."""

    instrument_id: uuid.UUID
    driver: str
    resource: str
    params: VnaSweepParams
    processor: str
    thickness_m: float | None


def execute_job(job_id: uuid.UUID, ctx: EngineContext) -> JobStatus | None:
    """Run one queued job to its end; returns the final status (None if it was not runnable).

    Raises :class:`InstrumentBusyError` if the instrument is locked by another job.
    """
    with ctx.session_factory() as session:
        spec = _load_spec(session, job_id)
        if spec is None:
            return None
        lock = InstrumentLock(ctx.redis, spec.instrument_id, ctx.lock_ttl_s)
        if not lock.acquire():
            raise InstrumentBusyError(str(spec.instrument_id))
        with structlog.contextvars.bound_contextvars(
            job_id=str(job_id), instrument_id=str(spec.instrument_id)
        ):
            try:
                return _run(session, job_id, spec, lock, ctx)
            finally:
                lock.release()


def _load_spec(session: Session, job_id: uuid.UUID) -> _JobSpec | None:
    job = session.get(Job, job_id)
    if job is None or job.status is not JobStatus.QUEUED:
        log.warning("job_not_runnable", job_id=str(job_id), status=job and job.status.value)
        return None
    instrument = session.get(Instrument, job.instrument_id)
    method = session.get(Method, job.method_id)
    assert instrument is not None
    assert method is not None
    sample = session.get(Sample, job.sample_id) if job.sample_id else None
    return _JobSpec(
        instrument_id=instrument.id,
        driver=instrument.driver,
        resource=instrument.resource,
        params=VnaSweepParams(**job.params),
        processor=method.processor,
        thickness_m=sample.thickness_mm / 1000 if sample else None,
    )


def _run(
    session: Session, job_id: uuid.UUID, spec: _JobSpec, lock: InstrumentLock, ctx: EngineContext
) -> JobStatus | None:
    now = _now()
    if not transition(
        session, job_id, JobStatus.QUEUED, JobStatus.RUNNING, started_at=now, heartbeat_at=now
    ):
        return None
    _set_instrument_status(session, spec.instrument_id, InstrumentStatus.BUSY)
    state = JobStatus.RUNNING
    log.info("job_started")
    try:
        if _measure(session, job_id, spec, lock, ctx):
            transition(session, job_id, JobStatus.RUNNING, JobStatus.CANCELLING)
            transition(
                session, job_id, JobStatus.CANCELLING, JobStatus.CANCELLED, finished_at=_now()
            )
            _finish(session, ctx, job_id, spec, InstrumentStatus.ONLINE)
            log.info("job_cancelled")
            return JobStatus.CANCELLED
        state = JobStatus.PROCESSING
        transition(session, job_id, JobStatus.RUNNING, JobStatus.PROCESSING)
        _process(session, job_id, spec)
        transition(session, job_id, JobStatus.PROCESSING, JobStatus.COMPLETED, finished_at=_now())
    except Exception as exc:
        code, message = _describe(exc)
        if code == "internal_error":
            log.error("job_crashed", exc_info=exc)
        failed = transition(
            session,
            job_id,
            state,
            JobStatus.FAILED,
            error_code=code,
            error_message=message,
            finished_at=_now(),
        )
        if not failed:  # cancelled by the API while the error happened
            transition(
                session, job_id, JobStatus.CANCELLING, JobStatus.CANCELLED, finished_at=_now()
            )
        _finish(session, ctx, job_id, spec, _instrument_status_after_error(code))
        log.warning("job_failed", error_code=code, error_message=message)
        return JobStatus.FAILED
    _finish(session, ctx, job_id, spec, InstrumentStatus.ONLINE)
    log.info("job_completed")
    return JobStatus.COMPLETED


def _finish(
    session: Session,
    ctx: EngineContext,
    job_id: uuid.UUID,
    spec: _JobSpec,
    instrument_status: InstrumentStatus,
) -> None:
    _set_instrument_status(session, spec.instrument_id, instrument_status)
    with contextlib.suppress(Exception):
        ctx.redis.delete(cancel_key(job_id))


def _instrument_status_after_error(code: str) -> InstrumentStatus:
    if code in {"instrument_connection", "instrument_timeout"}:
        return InstrumentStatus.OFFLINE
    if code == "instrument_error":
        return InstrumentStatus.ERROR
    return InstrumentStatus.ONLINE


def _set_instrument_status(
    session: Session, instrument_id: uuid.UUID, status: InstrumentStatus
) -> None:
    session.execute(update(Instrument).where(Instrument.id == instrument_id).values(status=status))
    session.commit()


class _Link:
    """The driver connection of one job; (re)opened on demand, dropped after failures."""

    def __init__(self, ctx: EngineContext, spec: _JobSpec) -> None:
        self._ctx = ctx
        self._spec = spec
        self._driver: InstrumentDriver | None = None

    def ensure(self) -> InstrumentDriver:
        if self._driver is None:
            driver = self._ctx.driver_factory(self._spec.driver, self._spec.resource)
            try:
                driver.connect()
                driver.reset()
                driver.configure(self._spec.params)
            except Exception:
                driver.close()
                raise
            self._driver = driver
        return self._driver

    def drop(self) -> None:
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.close()
            self._driver = None


def _saved_indices(session: Session, job_id: uuid.UUID) -> set[int]:
    return set(
        session.scalars(select(MeasurementPoint.idx).where(MeasurementPoint.job_id == job_id))
    )


def _cancel_requested(session: Session, ctx: EngineContext, job_id: uuid.UUID) -> bool:
    if ctx.redis.exists(cancel_key(job_id)):
        return True
    session.expire_all()
    return session.scalar(select(Job.status).where(Job.id == job_id)) is JobStatus.CANCELLING


def _measure(
    session: Session, job_id: uuid.UUID, spec: _JobSpec, lock: InstrumentLock, ctx: EngineContext
) -> bool:
    """Measure all segments that are not saved yet; True if the job was cancelled meanwhile."""
    saved = _saved_indices(session, job_id)
    segments = split_sweep(spec.params)
    complete = [
        s for s in segments if all(i in saved for i in range(s.first_idx, s.first_idx + s.count))
    ]
    if saved:
        log.info("job_resumed", points_saved=len(saved), segments_saved=len(complete))
        _update_progress(session, job_id, len(saved), len(complete))
    link = _Link(ctx, spec)
    try:
        segments_done = len(complete)
        for segment in segments:
            if segment in complete:
                continue
            if _cancel_requested(session, ctx, job_id):
                return True
            data = _measure_with_retries(link, segment, spec, lock, ctx)
            segments_done += 1
            _save_segment(session, job_id, data, segments_done)
            lock.extend()
    finally:
        link.drop()
    return False


def _measure_with_retries(
    link: _Link, segment: Segment, spec: _JobSpec, lock: InstrumentLock, ctx: EngineContext
) -> SegmentData:
    """Measure one segment; on a driver failure reconnect, back off 1, 2, 4 ... s and retry."""
    attempt = 0
    while True:
        try:
            return link.ensure().measure_segment(segment)
        except RETRYABLE as exc:
            link.drop()
            if attempt >= spec.params.max_retries:
                raise
            delay = min(2.0**attempt, MAX_BACKOFF_S)
            attempt += 1
            log.warning(
                "segment_retry",
                segment=segment.index,
                attempt=attempt,
                max_retries=spec.params.max_retries,
                delay_s=delay,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            _wait(delay, lock, ctx)


def _wait(seconds: float, lock: InstrumentLock, ctx: EngineContext) -> None:
    """Sleep in slices, keeping the instrument lock alive."""
    step = max(1.0, ctx.lock_ttl_s / 3)
    remaining = seconds
    while remaining > 0:
        chunk = min(step, remaining)
        ctx.sleep(chunk)
        remaining -= chunk
        lock.extend()


def _update_progress(
    session: Session, job_id: uuid.UUID, points_done: int, segments_done: int
) -> None:
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(points_done=points_done, segments_done=segments_done, heartbeat_at=_now())
    )
    session.commit()


def _save_segment(
    session: Session, job_id: uuid.UUID, data: SegmentData, segments_done: int
) -> None:
    """Store the points of one segment and advance progress in a single transaction.

    Re-saving a segment is harmless: ``(job_id, idx)`` is unique and conflicts are ignored.
    """
    now = _now()
    first = data.segment.first_idx
    rows = [
        {
            "job_id": job_id,
            "idx": first + i,
            "freq_hz": float(data.freq_hz[i]),
            "s11_re": float(data.s11[i].real),
            "s11_im": float(data.s11[i].imag),
            "s21_re": float(data.s21[i].real),
            "s21_im": float(data.s21[i].imag),
            "measured_at": now,
        }
        for i in range(data.segment.count)
    ]
    insert = _dialect_insert(session)
    session.execute(
        insert(MeasurementPoint)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["job_id", "idx"])
    )
    count = session.scalar(
        select(func.count()).select_from(MeasurementPoint).where(MeasurementPoint.job_id == job_id)
    )
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(points_done=count, segments_done=segments_done, heartbeat_at=now)
    )
    session.commit()
    log.debug("segment_saved", segment=data.segment.index, points=count)


def _dialect_insert(session: Session) -> Any:
    """Insert construct with ``ON CONFLICT DO NOTHING`` for the session's dialect."""
    if session.get_bind().dialect.name == "postgresql":
        return postgresql.insert
    return sqlite.insert


def _process(session: Session, job_id: uuid.UUID, spec: _JobSpec) -> None:
    rows = session.execute(
        select(
            MeasurementPoint.freq_hz,
            MeasurementPoint.s11_re,
            MeasurementPoint.s11_im,
            MeasurementPoint.s21_re,
            MeasurementPoint.s21_im,
        )
        .where(MeasurementPoint.job_id == job_id)
        .order_by(MeasurementPoint.idx)
    ).all()
    table = np.array(rows, dtype=np.float64).reshape(-1, 5)
    result = registry.get_processor(spec.processor).process(
        ProcessorInput(
            freq_hz=table[:, 0],
            s11=table[:, 1] + 1j * table[:, 2],
            s21=table[:, 3] + 1j * table[:, 4],
            thickness_m=spec.thickness_m,
        )
    )
    session.execute(update(Result).where(Result.job_id == job_id).values(is_current=False))
    session.add(
        Result(
            job_id=job_id,
            processor=result.processor,
            processor_version=result.version,
            data=_result_data(result),
            summary={key: _finite(value) for key, value in result.summary.items()},
            is_current=True,
        )
    )
    session.commit()


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _result_data(result: ProcessorResult) -> dict[str, Any]:
    return {
        "freq_hz": [float(f) for f in result.freq_hz],
        "columns": {
            name: [_finite(float(v)) for v in values] for name, values in result.columns.items()
        },
        "flags": {name: [bool(v) for v in values] for name, values in result.flags.items()},
        # JSONB does not keep key order, so the column order is stored explicitly
        "column_order": list(result.columns),
        "flag_order": list(result.flags),
    }


def _describe(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, InstrumentTimeoutError):
        return "instrument_timeout", str(exc)
    if isinstance(exc, InstrumentConnectionError):
        return "instrument_connection", str(exc)
    if isinstance(exc, InstrumentError):
        return "instrument_error", str(exc)
    if isinstance(exc, DriverError):
        return "driver_error", str(exc)
    if isinstance(exc, LockLostError):
        return "lock_lost", "Instrument lock was lost during the measurement"
    return "internal_error", f"{type(exc).__name__}: {exc}"


def _now() -> datetime:
    return datetime.now(UTC)
