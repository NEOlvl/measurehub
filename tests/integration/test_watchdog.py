"""Watchdog and instrument polling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus
from measurehub.db.models import Instrument, InstrumentStatus, Job
from measurehub.services.instruments import poll_instruments
from measurehub.worker.engine import EngineContext
from measurehub.worker.locks import InstrumentLock
from measurehub.worker.watchdog import requeue_stale_jobs
from simulator.server import SimulatorThread
from tests.integration.test_engine import load, make_job

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def stale_job(
    factory: sessionmaker[Session],
    port: int,
    *,
    status: JobStatus = JobStatus.RUNNING,
    heartbeat_age_s: float = 120,
    attempt: int = 0,
) -> uuid.UUID:
    job_id = make_job(factory, port, status=status)
    with factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.heartbeat_at = NOW - timedelta(seconds=heartbeat_age_s)
        job.attempt = attempt
        session.commit()
    return job_id


def run_watchdog(factory: sessionmaker[Session], sent: list[uuid.UUID]):  # type: ignore[no-untyped-def]
    return requeue_stale_jobs(factory, sent.append, stale_after_s=60, max_requeues=2, now=NOW)


def test_stale_running_job_is_requeued_and_dispatched(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = stale_job(session_factory, 1)
    sent: list[uuid.UUID] = []
    report = run_watchdog(session_factory, sent)
    assert report.requeued == [job_id]
    assert sent == [job_id]
    job = load(session_factory, job_id)
    assert job.status is JobStatus.QUEUED
    assert job.attempt == 1


def test_job_with_a_fresh_heartbeat_is_left_alone(session_factory: sessionmaker[Session]) -> None:
    job_id = stale_job(session_factory, 1, heartbeat_age_s=30)
    sent: list[uuid.UUID] = []
    report = run_watchdog(session_factory, sent)
    assert (report.requeued, report.failed, report.cancelled) == ([], [], [])
    assert sent == []
    assert load(session_factory, job_id).status is JobStatus.RUNNING


def test_other_statuses_are_ignored(session_factory: sessionmaker[Session]) -> None:
    ids = [
        stale_job(session_factory, 1, status=status)
        for status in (JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.COMPLETED)
    ]
    run_watchdog(session_factory, [])
    assert [load(session_factory, i).status for i in ids] == [
        JobStatus.QUEUED,
        JobStatus.PROCESSING,
        JobStatus.COMPLETED,
    ]


def test_a_job_is_requeued_at_most_twice_then_fails(session_factory: sessionmaker[Session]) -> None:
    job_id = stale_job(session_factory, 1, attempt=2)
    sent: list[uuid.UUID] = []
    report = run_watchdog(session_factory, sent)
    assert report.failed == [job_id]
    assert sent == []
    job = load(session_factory, job_id)
    assert job.status is JobStatus.FAILED
    assert job.error_code == "worker_lost"
    assert job.finished_at is not None


def test_stale_cancelling_job_becomes_cancelled(session_factory: sessionmaker[Session]) -> None:
    job_id = stale_job(session_factory, 1, status=JobStatus.CANCELLING)
    report = run_watchdog(session_factory, [])
    assert report.cancelled == [job_id]
    assert load(session_factory, job_id).status is JobStatus.CANCELLED


def test_dispatch_failure_fails_the_job_instead_of_losing_it(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = stale_job(session_factory, 1)

    def broken(_: uuid.UUID) -> None:
        raise ConnectionError("broker down")

    report = requeue_stale_jobs(session_factory, broken, stale_after_s=60, now=NOW)
    assert report.failed == [job_id]
    assert report.requeued == []
    assert load(session_factory, job_id).error_code == "dispatch_failed"


def test_two_watchdogs_do_not_requeue_the_same_job_twice(
    session_factory: sessionmaker[Session],
) -> None:
    stale_job(session_factory, 1)
    first: list[uuid.UUID] = []
    second: list[uuid.UUID] = []
    run_watchdog(session_factory, first)
    run_watchdog(session_factory, second)  # the job is queued by now
    assert len(first) == 1
    assert second == []


# -- instrument polling ----------------------------------------------------------------------------


def make_instrument(
    factory: sessionmaker[Session], port: int, *, status: InstrumentStatus, enabled: bool = True
) -> uuid.UUID:
    with factory() as session:
        instrument = Instrument(
            name=f"i-{uuid.uuid4().hex[:6]}",
            resource=f"TCPIP0::127.0.0.1::{port}::SOCKET",
            driver="sim_vna",
            status=status,
            is_enabled=enabled,
        )
        session.add(instrument)
        session.commit()
        return instrument.id


def status_of(factory: sessionmaker[Session], instrument_id: uuid.UUID) -> InstrumentStatus:
    with factory() as session:
        instrument = session.get(Instrument, instrument_id)
        assert instrument is not None
        return instrument.status


def test_poll_updates_status_idn_and_last_seen(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    online = make_instrument(session_factory, sim.port, status=InstrumentStatus.OFFLINE)
    offline = make_instrument(session_factory, 1, status=InstrumentStatus.ONLINE)

    result = poll_instruments(session_factory, engine_ctx.redis, engine_ctx.driver_factory)

    assert result == {online: InstrumentStatus.ONLINE, offline: InstrumentStatus.OFFLINE}
    with session_factory() as session:
        instrument = session.get(Instrument, online)
        assert instrument is not None
        assert instrument.idn == "MeasureHub,SIM-VNA-01,000001,1.0"
        assert instrument.last_seen_at is not None


def test_poll_skips_busy_locked_and_disabled_instruments(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    busy = make_instrument(session_factory, sim.port, status=InstrumentStatus.BUSY)
    locked = make_instrument(session_factory, sim.port, status=InstrumentStatus.OFFLINE)
    disabled = make_instrument(
        session_factory, sim.port, status=InstrumentStatus.OFFLINE, enabled=False
    )
    lock = InstrumentLock(engine_ctx.redis, locked, 30)
    assert lock.acquire()

    result = poll_instruments(session_factory, engine_ctx.redis, engine_ctx.driver_factory)

    assert result == {}
    assert status_of(session_factory, busy) is InstrumentStatus.BUSY
    assert status_of(session_factory, locked) is InstrumentStatus.OFFLINE
    assert status_of(session_factory, disabled) is InstrumentStatus.OFFLINE
