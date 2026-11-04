"""Job engine: take the instrument, measure segment by segment, process, store."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import numpy as np
import structlog
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus, can_transition
from measurehub.db.models import Instrument, Job, MeasurementPoint, Method, Result, Sample
from measurehub.drivers.errors import (
    DriverError,
    InstrumentConnectionError,
    InstrumentError,
    InstrumentTimeoutError,
)
from measurehub.drivers.models import SegmentData, VnaSweepParams
from measurehub.drivers.registry import DriverFactory
from measurehub.drivers.segments import split_sweep
from measurehub.processing import registry
from measurehub.processing.base import ProcessorInput, ProcessorResult
from measurehub.worker.locks import InstrumentLock, LockLostError

log = structlog.get_logger()


class InstrumentBusyError(Exception):
    """Another job holds the instrument; the task should be retried later."""


@dataclass(frozen=True, slots=True)
class EngineContext:
    session_factory: sessionmaker[Session]
    redis: Any
    driver_factory: DriverFactory
    lock_ttl_s: int = 30


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
    """Run one queued job to completion; returns its final status (None if it was not runnable).

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
    now = datetime.now(UTC)
    if not _transition(
        session, job_id, JobStatus.QUEUED, JobStatus.RUNNING, started_at=now, heartbeat_at=now
    ):
        return None
    state = JobStatus.RUNNING
    log.info("job_started")
    try:
        _measure(session, job_id, spec, lock, ctx)
        state = JobStatus.PROCESSING
        _transition(session, job_id, JobStatus.RUNNING, JobStatus.PROCESSING)
        _process(session, job_id, spec)
        _transition(session, job_id, JobStatus.PROCESSING, JobStatus.COMPLETED, finished_at=_now())
    except Exception as exc:
        code, message = _describe(exc)
        if code == "internal_error":
            log.error("job_crashed", exc_info=exc)
        _transition(
            session,
            job_id,
            state,
            JobStatus.FAILED,
            error_code=code,
            error_message=message,
            finished_at=_now(),
        )
        log.warning("job_failed", error_code=code, error_message=message)
        return JobStatus.FAILED
    log.info("job_completed")
    return JobStatus.COMPLETED


def _measure(
    session: Session, job_id: uuid.UUID, spec: _JobSpec, lock: InstrumentLock, ctx: EngineContext
) -> None:
    driver = ctx.driver_factory(spec.driver, spec.resource)
    try:
        driver.connect()
        driver.reset()
        driver.configure(spec.params)
        for segment in split_sweep(spec.params):
            data = driver.measure_segment(segment)
            _save_segment(session, job_id, data)
            lock.extend()
    finally:
        driver.close()


def _save_segment(session: Session, job_id: uuid.UUID, data: SegmentData) -> None:
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
        .values(points_done=count, segments_done=data.segment.index + 1, heartbeat_at=now)
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


def _transition(
    session: Session, job_id: uuid.UUID, old: JobStatus, new: JobStatus, **values: Any
) -> bool:
    """Atomic ``UPDATE ... WHERE status = old``; False if the job was not in ``old``."""
    assert can_transition(old, new), f"{old} -> {new} is not allowed"
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Job).where(Job.id == job_id, Job.status == old).values(status=new, **values)
        ),
    )
    session.commit()
    return result.rowcount == 1


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
