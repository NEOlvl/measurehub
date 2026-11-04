"""Retry, resume and cancel behaviour of the engine in detail."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus
from measurehub.db.models import Instrument, InstrumentStatus, Job, MeasurementPoint
from measurehub.drivers.errors import InstrumentConnectionError, InstrumentError
from measurehub.drivers.models import Segment, VnaSweepParams
from measurehub.drivers.segments import split_sweep
from measurehub.worker.cancel import cancel_key
from measurehub.worker.engine import EngineContext, _save_segment, execute_job
from simulator.server import SimulatorThread
from tests.integration.test_engine import load, make_job

SWEEP = VnaSweepParams(points=100, segment_size=25, max_retries=3)


class FailingDriver:
    """Wraps a real driver; the listed segment indexes fail a given number of times."""

    def __init__(self, inner: Any, failures: dict[int, list[Any]]) -> None:
        self._inner = inner
        self._failures = failures

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def measure_segment(self, segment: Segment) -> Any:
        pending = self._failures.get(segment.index)
        if pending:
            raise pending.pop(0)
        return self._inner.measure_segment(segment)


def with_failures(
    ctx: EngineContext, failures: dict[int, list[Any]], connections: list[object]
) -> EngineContext:
    real = ctx.driver_factory

    def factory(key: str, resource: str) -> Any:
        driver = FailingDriver(real(key, resource), failures)
        connections.append(driver)
        return driver

    return replace(ctx, driver_factory=factory)


def test_backoff_is_exponential_and_the_link_is_reopened_each_time(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
    sleeps: list[float],
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    connections: list[object] = []
    failures = {
        1: [
            InstrumentConnectionError("a"),
            InstrumentConnectionError("b"),
            InstrumentError(-1, "c"),
        ]
    }
    ctx = with_failures(engine_ctx, failures, connections)

    assert execute_job(job_id, ctx) is JobStatus.COMPLETED

    assert sleeps == [1.0, 2.0, 4.0]
    assert len(connections) == 1 + 3  # the first connection plus one per retry
    assert load(session_factory, job_id).points_done == 100


def test_retries_restart_for_every_segment(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
    sleeps: list[float],
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    failures = {
        0: [InstrumentConnectionError("x")] * 3,
        2: [InstrumentConnectionError("y")] * 3,
    }
    ctx = with_failures(engine_ctx, failures, [])
    assert execute_job(job_id, ctx) is JobStatus.COMPLETED
    assert sleeps == [1.0, 2.0, 4.0, 1.0, 2.0, 4.0]


def test_zero_retries_fail_immediately(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
    sleeps: list[float],
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP.model_copy(update={"max_retries": 0}))
    ctx = with_failures(engine_ctx, {0: [InstrumentError(-230, "stale")]}, [])
    assert execute_job(job_id, ctx) is JobStatus.FAILED
    assert sleeps == []
    job = load(session_factory, job_id)
    assert job.error_code == "instrument_error"
    with session_factory() as session:
        assert session.scalars(select(Instrument.status)).one() is InstrumentStatus.ERROR


def test_long_backoff_is_slept_in_slices_that_keep_the_lock_alive(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
    sleeps: list[float],
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP.model_copy(update={"max_retries": 10}))
    failures = {0: [InstrumentConnectionError("x")] * 6}  # delays 1, 2, 4, 8, 16, 30
    ctx = replace(with_failures(engine_ctx, failures, []), lock_ttl_s=9)  # slices of 3 s
    assert execute_job(job_id, ctx) is JobStatus.COMPLETED
    assert max(sleeps) == 3.0
    assert sum(sleeps) == pytest.approx(1 + 2 + 4 + 8 + 16 + 30)


# -- resume ----------------------------------------------------------------------------------------


def save_segments(
    ctx: EngineContext,
    factory: sessionmaker[Session],
    sim: SimulatorThread,
    job_id: uuid.UUID,
    indexes: list[int],
) -> None:
    """Pre-store segments as a worker that died after them would have."""
    params = VnaSweepParams(**load(factory, job_id).params)
    driver = ctx.driver_factory("sim_vna", f"TCPIP0::127.0.0.1::{sim.port}::SOCKET")
    driver.connect()
    try:
        driver.reset()
        driver.configure(params)
        segments = split_sweep(params)
        with factory() as session:
            for done, index in enumerate(indexes, start=1):
                _save_segment(session, job_id, driver.measure_segment(segments[index]), done)
    finally:
        driver.close()


def test_job_resumes_from_the_first_unsaved_segment(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    save_segments(engine_ctx, session_factory, sim, job_id, [0, 1])
    sweeps_before = sim.server.engine.sweeps

    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED

    assert sim.server.engine.sweeps - sweeps_before == 2  # only segments 2 and 3
    job = load(session_factory, job_id)
    assert (job.points_done, job.segments_done) == (100, 4)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MeasurementPoint)) == 100


def test_a_partially_saved_segment_is_measured_again_without_duplicates(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    save_segments(engine_ctx, session_factory, sim, job_id, [0, 1])
    with (
        session_factory() as session
    ):  # lose the tail of segment 1 (e.g. an older, non-atomic save)
        session.query(MeasurementPoint).filter(MeasurementPoint.idx >= 40).delete()
        session.commit()
    sweeps_before = sim.server.engine.sweeps

    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED

    assert sim.server.engine.sweeps - sweeps_before == 3  # segments 1, 2 and 3
    with session_factory() as session:
        indexes = session.scalars(
            select(MeasurementPoint.idx).where(MeasurementPoint.job_id == job_id)
        ).all()
    assert sorted(indexes) == list(range(100))


# -- cancel ----------------------------------------------------------------------------------------


def test_cancel_flag_set_before_start_cancels_without_measuring(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    engine_ctx.redis.set(cancel_key(job_id), "1")

    assert execute_job(job_id, engine_ctx) is JobStatus.CANCELLED

    job = load(session_factory, job_id)
    assert job.status is JobStatus.CANCELLED
    assert job.points_done == 0
    assert job.finished_at is not None
    assert not engine_ctx.redis.exists(cancel_key(job_id))
    with session_factory() as session:
        assert session.scalars(select(Instrument.status)).one() is InstrumentStatus.ONLINE


def test_cancel_during_a_segment_finishes_that_segment_first(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    real = engine_ctx.driver_factory

    def factory(key: str, resource: str) -> Any:
        driver = real(key, resource)
        original = driver.measure_segment

        def measure(segment: Segment) -> Any:
            data = original(segment)
            if segment.index == 1:  # the request arrives while segment 1 is being measured
                engine_ctx.redis.set(cancel_key(job_id), "1")
            return data

        driver.measure_segment = measure  # type: ignore[method-assign]
        return driver

    assert execute_job(job_id, replace(engine_ctx, driver_factory=factory)) is JobStatus.CANCELLED
    job = load(session_factory, job_id)
    assert (job.points_done, job.segments_done) == (50, 2)  # segments 0 and 1 are kept


def test_cancelling_status_in_the_database_is_honoured_without_the_flag(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    real = engine_ctx.driver_factory

    def factory(key: str, resource: str) -> Any:
        driver = real(key, resource)
        original = driver.measure_segment

        def measure(segment: Segment) -> Any:
            data = original(segment)
            if segment.index == 0:  # the API moved the job to cancelling, the Redis flag is lost
                with session_factory() as session:
                    session.get(Job, job_id).status = JobStatus.CANCELLING  # type: ignore[union-attr]
                    session.commit()
            return data

        driver.measure_segment = measure  # type: ignore[method-assign]
        return driver

    assert execute_job(job_id, replace(engine_ctx, driver_factory=factory)) is JobStatus.CANCELLED
    assert load(session_factory, job_id).points_done == 25
