"""The four fault-tolerance scenarios of the specification (section 11.5).

1. 10% of the exchanges drop the connection -> the job still completes;
2. the worker is killed in the middle -> the job continues without losing or duplicating points;
3. the instrument hangs -> timeout and retry;
4. cancellation in the middle -> measured points are kept, the instrument is released.

In every scenario the stored points are compared with the exact physical model (the simulator
runs without noise), so lost, duplicated, shifted or corrupted points would be noticed.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import fakeredis
import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.settings import Settings
from measurehub.core.states import JobStatus
from measurehub.db.models import Instrument, InstrumentStatus, MeasurementPoint, Result
from measurehub.drivers.base import InstrumentDriver
from measurehub.drivers.models import VnaSweepParams
from measurehub.drivers.registry import DriverFactory
from measurehub.drivers.segments import split_sweep
from measurehub.worker.engine import EngineContext, InstrumentBusyError, execute_job
from measurehub.worker.locks import InstrumentLock
from measurehub.worker.watchdog import requeue_stale_jobs
from simulator.model import slab_sparameters
from simulator.scpi import ScpiEngine
from simulator.server import SimulatorThread
from tests.conftest import SIM_SAMPLE, make_fixtures
from tests.integration.test_engine import load, make_job

SWEEP = VnaSweepParams(points=200, segment_size=20, max_retries=10)


@pytest.fixture
def exact_sim() -> Iterator[Callable[..., SimulatorThread]]:
    """Factory of noiseless simulators (so stored points can be compared with the model)."""
    started: list[SimulatorThread] = []

    def start(**faults: float) -> SimulatorThread:
        engine = ScpiEngine(SIM_SAMPLE, noise_k=0.0, time_scale=0.0, **faults)  # type: ignore[arg-type]
        thread = SimulatorThread(engine)
        thread.start()
        started.append(thread)
        return thread

    yield start
    for thread in started:
        thread.stop()


def assert_complete_and_exact(factory: sessionmaker[Session], job_id: uuid.UUID) -> None:
    """Exactly one point per index, none missing, every value equal to the model."""
    job = load(factory, job_id)
    assert job.status is JobStatus.COMPLETED
    assert (job.points_done, job.points_total) == (SWEEP.points, SWEEP.points)
    with factory() as session:
        rows = session.execute(
            select(
                MeasurementPoint.idx,
                MeasurementPoint.freq_hz,
                MeasurementPoint.s11_re,
                MeasurementPoint.s11_im,
                MeasurementPoint.s21_re,
                MeasurementPoint.s21_im,
            )
            .where(MeasurementPoint.job_id == job_id)
            .order_by(MeasurementPoint.idx)
        ).all()
        assert session.scalar(select(func.count()).select_from(Result)) == 1
    table = np.array(rows, dtype=np.float64)
    assert table[:, 0].astype(int).tolist() == list(range(SWEEP.points))  # no gaps, no duplicates
    expected_s11, expected_s21 = slab_sparameters(table[:, 1], SIM_SAMPLE)
    assert np.allclose(table[:, 2] + 1j * table[:, 3], expected_s11, atol=1e-8)
    assert np.allclose(table[:, 4] + 1j * table[:, 5], expected_s21, atol=1e-8)


# -- 1. dropped connections ---------------------------------------------------------------------


def test_job_survives_ten_percent_dropped_connections(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    exact_sim: Callable[..., SimulatorThread],
    sleeps: list[float],
) -> None:
    sim = exact_sim(fault_drop=0.1, fault_seed=7)
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)

    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED

    assert sim.server.engine.faults_injected["drop"] >= 3  # faults really happened
    assert sleeps  # ... and were retried with a backoff
    assert 1.0 in sleeps
    assert max(sleeps) <= 10.0  # long backoffs are slept in slices of lock_ttl / 3
    assert_complete_and_exact(session_factory, job_id)


def test_instrument_errors_are_retried_too(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    exact_sim: Callable[..., SimulatorThread],
) -> None:
    sim = exact_sim(fault_err=0.1, fault_seed=3)
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED
    assert sim.server.engine.faults_injected["err"] >= 3
    assert_complete_and_exact(session_factory, job_id)


def test_retries_are_bounded_and_measured_points_are_kept(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    exact_sim: Callable[..., SimulatorThread],
    sleeps: list[float],
) -> None:
    sim = exact_sim()
    job_id = make_job(session_factory, sim.port, sweep=SWEEP.model_copy(update={"max_retries": 3}))
    real_factory = engine_ctx.driver_factory

    def dying_factory(key: str, resource: str) -> InstrumentDriver:
        driver = real_factory(key, resource)
        original = driver.measure_segment

        def measure(segment):  # type: ignore[no-untyped-def]
            if segment.index >= 2:  # the link is gone for good after two segments
                sim.server.engine.fault_drop = 1.0
            return original(segment)

        driver.measure_segment = measure  # type: ignore[method-assign]
        return driver

    ctx = replace(engine_ctx, driver_factory=dying_factory)
    assert execute_job(job_id, ctx) is JobStatus.FAILED

    job = load(session_factory, job_id)
    # a dropped connection is seen as a closed socket or as a timeout, depending on timing
    assert job.error_code in {"instrument_connection", "instrument_timeout"}
    assert sleeps == [1.0, 2.0, 4.0]  # 1, 2, 4 ... and then max_retries is exhausted
    assert (job.points_done, job.segments_done) == (40, 2)  # NFR-05: nothing measured is lost
    with session_factory() as session:
        instrument = session.scalars(select(Instrument)).one()
        assert instrument.status is InstrumentStatus.OFFLINE


# -- 3. hanging instrument ------------------------------------------------------------------------


def test_hanging_instrument_times_out_and_the_segment_is_retried(
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    exact_sim: Callable[..., SimulatorThread],
    sleeps: list[float],
) -> None:
    sim = exact_sim(fault_hang=0.03, fault_seed=5)
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)

    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED

    assert sim.server.engine.faults_injected["hang"] >= 1
    assert sleeps
    assert_complete_and_exact(session_factory, job_id)


# -- 2. worker killed in the middle ---------------------------------------------------------------


@pytest.fixture
def tcp_redis() -> Iterator[int]:
    """A Redis-compatible server on a real port, shared by this process and the child."""
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    server.block_on_close = False  # open client connections must not block the teardown
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def test_killed_worker_resumes_without_losing_or_duplicating_points(
    settings: Settings,
    session_factory: sessionmaker[Session],
    tcp_redis: int,
    sleeps: list[float],
) -> None:
    import redis

    sim_engine = ScpiEngine(SIM_SAMPLE, noise_k=0.0, time_scale=1.0)  # 0.2 s per segment
    with SimulatorThread(sim_engine) as sim:
        sweep = SWEEP.model_copy(update={"ifbw_hz": 100.0})
        job_id = make_job(session_factory, sim.port, sweep=sweep)
        # fakeredis' TCP server stalls ~2 s on some first commands, so the TTL must exceed that
        lock_ttl_s = 6
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("worker_process.py")),
                settings.sync_database_url,
                str(tcp_redis),
                str(job_id),
                str(lock_ttl_s),
            ]
        )
        try:
            deadline = time.monotonic() + 90
            while load(session_factory, job_id).points_done < 60:
                assert worker.poll() is None, "worker exited before it could be killed"
                assert time.monotonic() < deadline, "worker made no progress"
                time.sleep(0.02)
        finally:
            worker.kill()  # SIGKILL / TerminateProcess: no cleanup code runs
            worker.wait(timeout=30)

        crashed = load(session_factory, job_id)
        assert crashed.status is JobStatus.RUNNING  # nobody noticed yet
        saved = crashed.points_done
        assert 60 <= saved < sweep.points
        saved_segments = saved // sweep.segment_size

        # the watchdog notices the missing heartbeat and re-queues the job
        time.sleep(1.0)
        requeued: list[uuid.UUID] = []
        report = requeue_stale_jobs(
            session_factory, requeued.append, stale_after_s=0.5, max_requeues=2
        )
        assert report.requeued == [job_id] == requeued
        assert load(session_factory, job_id).status is JobStatus.QUEUED
        assert load(session_factory, job_id).attempt == 1

        # a new worker takes over; the dead one's lock expires after its TTL
        ctx = EngineContext(
            session_factory=session_factory,
            redis=redis.Redis(port=tcp_redis),
            driver_factory=_driver_factory(),
            lock_ttl_s=lock_ttl_s,
        )
        sweeps_before = sim_engine.sweeps
        status = _execute_when_free(job_id, ctx)

        assert status is JobStatus.COMPLETED
        total_segments = len(split_sweep(sweep))
        # only the segments that were not saved are measured again
        assert sim_engine.sweeps - sweeps_before == total_segments - saved_segments
        assert_complete_and_exact(session_factory, job_id)
        assert load(session_factory, job_id).attempt == 1
    assert sleeps == []  # no retries were needed in the second run


def _driver_factory() -> DriverFactory:
    from measurehub.drivers.registry import create_driver

    return lambda key, resource: create_driver(key, resource, timeout_ms=3000, connect_attempts=1)


def _execute_when_free(job_id: uuid.UUID, ctx: EngineContext) -> JobStatus | None:
    deadline = time.monotonic() + 30
    while True:
        try:
            return execute_job(job_id, ctx)
        except InstrumentBusyError:
            assert time.monotonic() < deadline, "the dead worker's lock never expired"
            time.sleep(0.2)


# -- 4. cancel in the middle ----------------------------------------------------------------------


async def test_cancel_stops_after_the_current_segment_and_keeps_the_points(
    client: httpx.AsyncClient,
    app: FastAPI,
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
) -> None:
    sim_engine = ScpiEngine(SIM_SAMPLE, noise_k=0.0, time_scale=1.0)
    with SimulatorThread(sim_engine) as sim:
        ids = await make_fixtures(
            client,
            sim.port,
            method_params={"points": 200, "segment_size": 20, "ifbw_hz": 100.0},
        )
        runner: list[threading.Thread] = []

        def start_in_background(job_id: uuid.UUID) -> str:
            thread = threading.Thread(target=execute_job, args=(job_id, engine_ctx), daemon=True)
            thread.start()
            runner.append(thread)
            return "thread"

        app.state.dispatcher = start_in_background
        job = (await client.post("/api/v1/jobs", json=ids)).json()

        for _ in range(500):  # wait until a few segments are measured
            state = (await client.get(f"/api/v1/jobs/{job['id']}")).json()
            if state["points_done"] >= 40:
                break
            await _sleep(0.02)
        assert state["status"] == "running"

        cancelled = await client.post(f"/api/v1/jobs/{job['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] in {"cancelling", "cancelled"}

        runner[0].join(timeout=30)
        assert not runner[0].is_alive()

    final = (await client.get(f"/api/v1/jobs/{job['id']}")).json()
    assert final["status"] == "cancelled"
    assert final["finished_at"]
    assert 40 <= final["points_done"] < 200  # stopped early, measured points are kept
    assert final["points_done"] % 20 == 0  # ... and only whole segments (the current one finished)

    with session_factory() as session:
        stored = session.scalar(select(func.count()).select_from(MeasurementPoint))
        assert stored == final["points_done"]
        assert session.scalars(select(Instrument.status)).one() is InstrumentStatus.ONLINE
        assert session.scalar(select(func.count()).select_from(Result)) == 0
    assert InstrumentLock(engine_ctx.redis, uuid.UUID(ids["instrument_id"]), 30).acquire()
    assert not engine_ctx.redis.exists(f"cancel:job:{job['id']}")  # the flag is cleaned up
    assert (await client.post(f"/api/v1/jobs/{job['id']}/cancel")).status_code == 200  # idempotent
    assert (await client.get(f"/api/v1/jobs/{job['id']}/result")).status_code == 409
    assert load(session_factory, uuid.UUID(job["id"])).status is JobStatus.CANCELLED


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
