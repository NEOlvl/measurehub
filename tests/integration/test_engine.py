"""Job engine against SQLite, fake Redis and a real simulator over pyvisa-py."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus
from measurehub.db.models import Instrument, Job, MeasurementPoint, Method, Result, Sample
from measurehub.drivers.errors import InstrumentError
from measurehub.drivers.models import VnaSweepParams
from measurehub.drivers.segments import split_sweep
from measurehub.worker.engine import (
    EngineContext,
    InstrumentBusyError,
    _save_segment,
    execute_job,
)
from measurehub.worker.locks import InstrumentLock, LockLostError
from simulator.server import SimulatorThread

PARAMS = VnaSweepParams(points=100, segment_size=25).model_dump()


def make_job(
    factory: sessionmaker[Session],
    port: int,
    *,
    processor: str = "nrw",
    status: JobStatus = JobStatus.QUEUED,
    sweep: VnaSweepParams | None = None,
) -> uuid.UUID:
    params = (sweep or VnaSweepParams(**PARAMS)).model_dump()
    with factory() as session:
        instrument = Instrument(
            name=f"i-{uuid.uuid4().hex[:6]}",
            resource=f"TCPIP0::127.0.0.1::{port}::SOCKET",
            driver="sim_vna",
        )
        method = Method(
            name=f"m-{uuid.uuid4().hex[:6]}",
            measurement_type="vna_sweep",
            params=params,
            processor=processor,
        )
        sample = Sample(code=f"s-{uuid.uuid4().hex[:6]}", material="x", thickness_mm=3.0)
        session.add_all([instrument, method, sample])
        session.flush()
        job = Job(
            method_id=method.id,
            instrument_id=instrument.id,
            sample_id=sample.id,
            params=params,
            status=status,
            points_total=params["points"],
            segments_total=len(split_sweep(VnaSweepParams(**params))),
        )
        session.add(job)
        session.commit()
        return job.id


def load(factory: sessionmaker[Session], job_id: uuid.UUID) -> Job:
    with factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def test_job_completes_and_stores_points_and_result(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port)
    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED

    job = load(session_factory, job_id)
    assert job.status is JobStatus.COMPLETED
    assert (job.points_done, job.segments_done) == (100, 4)
    with session_factory() as session:
        idx = session.scalars(
            select(MeasurementPoint.idx)
            .where(MeasurementPoint.job_id == job_id)
            .order_by(MeasurementPoint.idx)
        ).all()
        assert list(idx) == list(range(100))
        results = session.scalars(select(Result).where(Result.job_id == job_id)).all()
        assert len(results) == 1
        assert results[0].is_current
        assert results[0].processor_version == "1.0"


def test_lock_is_released_after_the_job(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    first = make_job(session_factory, sim.port)
    execute_job(first, engine_ctx)
    with session_factory() as session:
        instrument_id = session.get(Job, first).instrument_id  # type: ignore[union-attr]
    assert InstrumentLock(engine_ctx.redis, instrument_id, 30).acquire()


def test_busy_instrument_keeps_the_job_queued(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port)
    instrument_id = load(session_factory, job_id).instrument_id
    other = InstrumentLock(engine_ctx.redis, instrument_id, 30)
    assert other.acquire()

    with pytest.raises(InstrumentBusyError):
        execute_job(job_id, engine_ctx)
    assert load(session_factory, job_id).status is JobStatus.QUEUED

    other.release()
    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED


def test_only_one_job_per_instrument_at_a_time(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    """While a job measures, a second job for the same instrument is refused by the lock."""
    first = make_job(session_factory, sim.port)
    second = make_job(session_factory, sim.port)
    with session_factory() as session:  # point both jobs at the same instrument
        instrument_id = session.get(Job, first).instrument_id  # type: ignore[union-attr]
        session.get(Job, second).instrument_id = instrument_id  # type: ignore[union-attr]
        session.commit()

    outcomes: list[str] = []
    real_factory = engine_ctx.driver_factory

    def spying_factory(key: str, resource: str) -> Any:
        driver = real_factory(key, resource)
        original = driver.measure_segment

        def measure(segment: Any) -> Any:
            if segment.index == 1:  # mid-measurement of the first job
                try:
                    execute_job(second, engine_ctx)
                except InstrumentBusyError:
                    outcomes.append("busy")
            return original(segment)

        driver.measure_segment = measure  # type: ignore[method-assign]
        return driver

    ctx = replace(engine_ctx, driver_factory=spying_factory)
    assert execute_job(first, ctx) is JobStatus.COMPLETED
    assert outcomes == ["busy"]
    assert load(session_factory, second).status is JobStatus.QUEUED


def test_non_queued_or_missing_job_is_ignored(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    running = make_job(session_factory, sim.port, status=JobStatus.RUNNING)
    assert execute_job(running, engine_ctx) is None
    assert load(session_factory, running).status is JobStatus.RUNNING
    assert execute_job(uuid.uuid4(), engine_ctx) is None


def test_unreachable_instrument_fails_the_job_and_releases_the_lock(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session]
) -> None:
    job_id = make_job(session_factory, 1)
    assert execute_job(job_id, engine_ctx) is JobStatus.FAILED
    job = load(session_factory, job_id)
    assert job.error_code == "instrument_connection"
    assert job.finished_at is not None
    assert InstrumentLock(engine_ctx.redis, job.instrument_id, 30).acquire()


def test_instrument_error_mid_measurement_fails_the_job_and_keeps_saved_points(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port)
    real_factory = engine_ctx.driver_factory

    def failing_factory(key: str, resource: str) -> Any:
        driver = real_factory(key, resource)
        original = driver.measure_segment

        def measure(segment: Any) -> Any:
            if segment.index == 2:
                raise InstrumentError(-230, "Data corrupt or stale")
            return original(segment)

        driver.measure_segment = measure  # type: ignore[method-assign]
        return driver

    ctx = replace(engine_ctx, driver_factory=failing_factory)
    assert execute_job(job_id, ctx) is JobStatus.FAILED
    job = load(session_factory, job_id)
    assert job.error_code == "instrument_error"
    assert "-230" in (job.error_message or "")
    assert (job.points_done, job.segments_done) == (50, 2)  # NFR-05: measured points are kept


def test_internal_error_is_recorded_not_raised(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, processor="raw")
    with session_factory() as session:
        method = session.scalars(select(Method)).one()
        method.processor = "does-not-exist"
        session.commit()
    assert execute_job(job_id, engine_ctx) is JobStatus.FAILED
    job = load(session_factory, job_id)
    assert job.error_code == "internal_error"
    assert "does-not-exist" in (job.error_message or "")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MeasurementPoint)) == 100


def test_lock_loss_fails_the_job(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port)
    real_factory = engine_ctx.driver_factory

    def stealing_factory(key: str, resource: str) -> Any:
        engine_ctx.redis.flushall()  # the lock key vanishes, as if its TTL had expired
        return real_factory(key, resource)

    ctx = replace(engine_ctx, driver_factory=stealing_factory)
    assert execute_job(job_id, ctx) is JobStatus.FAILED
    assert load(session_factory, job_id).error_code == "lock_lost"


def test_lock_extend_raises_when_not_owned(engine_ctx: EngineContext) -> None:
    lock = InstrumentLock(engine_ctx.redis, uuid.uuid4(), 30)
    assert lock.acquire()
    engine_ctx.redis.flushall()
    with pytest.raises(LockLostError):
        lock.extend()


def test_saving_a_segment_twice_does_not_duplicate_points(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port)
    params = VnaSweepParams(**PARAMS)
    driver = engine_ctx.driver_factory("sim_vna", f"TCPIP0::127.0.0.1::{sim.port}::SOCKET")
    driver.connect()
    try:
        driver.reset()
        driver.configure(params)
        data = driver.measure_segment(split_sweep(params)[0])
    finally:
        driver.close()

    with session_factory() as session:
        _save_segment(session, job_id, data, 1)
        _save_segment(session, job_id, data, 1)
        assert session.scalar(select(func.count()).select_from(MeasurementPoint)) == 25
    job = load(session_factory, job_id)
    assert job.points_done == 25
    assert job.segments_done == 1
