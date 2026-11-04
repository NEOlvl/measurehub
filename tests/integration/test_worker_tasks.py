"""Celery wiring: task body, busy-instrument retry, reliability settings, dispatcher."""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace

import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session, sessionmaker

from measurehub.api.app import app_factory, celery_dispatcher
from measurehub.core import settings as settings_module
from measurehub.core.states import JobStatus
from measurehub.worker.engine import EngineContext
from measurehub.worker.locks import InstrumentLock
from simulator.server import SimulatorThread
from tests.integration.test_engine import load, make_job


@pytest.fixture
def tasks(monkeypatch: pytest.MonkeyPatch, engine_ctx: EngineContext) -> Iterator[ModuleType]:
    monkeypatch.setenv("MH_DATABASE_URL", "sqlite+aiosqlite:///unused.db")
    monkeypatch.setenv("MH_SECRET_KEY", "0123456789abcdef")
    settings_module.get_settings.cache_clear()
    module = importlib.import_module("measurehub.worker.tasks")
    monkeypatch.setattr(module, "get_context", lambda: engine_ctx)
    yield module
    settings_module.get_settings.cache_clear()


def test_celery_app_has_the_reliability_settings(tasks: ModuleType) -> None:
    conf = tasks.celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert "measurehub.run_job" in tasks.celery_app.tasks


def test_run_job_measures_the_job(
    tasks: ModuleType,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
) -> None:
    job_id = make_job(session_factory, sim.port)
    assert tasks.run_job.run(str(job_id)) == "completed"
    assert load(session_factory, job_id).status is JobStatus.COMPLETED
    assert tasks.run_job.run(str(job_id)) is None  # already done: a redelivery is a no-op


def test_run_job_retries_while_the_instrument_is_busy(
    monkeypatch: pytest.MonkeyPatch,
    tasks: ModuleType,
    engine_ctx: EngineContext,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
) -> None:
    job_id = make_job(session_factory, sim.port)
    lock = InstrumentLock(engine_ctx.redis, load(session_factory, job_id).instrument_id, 30)
    assert lock.acquire()
    retries: list[dict[str, object]] = []

    def fake_retry(**kwargs: object) -> Retry:
        retries.append(kwargs)
        return Retry("busy")

    monkeypatch.setattr(tasks.run_job, "retry", fake_retry)
    with pytest.raises(Retry):
        tasks.run_job.run(str(job_id))
    assert retries == [{"countdown": tasks.BUSY_RETRY_S}]
    assert load(session_factory, job_id).status is JobStatus.QUEUED


def test_default_dispatcher_enqueues_the_celery_task(
    tasks: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, ...]] = []

    def delay(*args: str) -> SimpleNamespace:
        sent.append(args)
        return SimpleNamespace(id="task-42")

    monkeypatch.setattr(tasks.run_job, "delay", delay)
    job_id = uuid.uuid4()
    assert celery_dispatcher(job_id) == "task-42"
    assert sent == [(str(job_id),)]


def test_app_factory_builds_the_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MH_DATABASE_URL", "sqlite+aiosqlite:///unused.db")
    monkeypatch.setenv("MH_SECRET_KEY", "0123456789abcdef")
    settings_module.get_settings.cache_clear()
    try:
        app = app_factory()
        assert {"/health/live", "/api/v1/jobs"} <= set(app.openapi()["paths"])
    finally:
        settings_module.get_settings.cache_clear()


def test_beat_schedules_the_watchdog_and_the_instrument_poll(tasks: ModuleType) -> None:
    schedule = tasks.celery_app.conf.beat_schedule
    assert schedule["watchdog"] == {"task": "measurehub.watchdog", "schedule": 30.0}
    assert schedule["poll-instruments"] == {"task": "measurehub.poll_instruments", "schedule": 60.0}
    assert {"measurehub.watchdog", "measurehub.poll_instruments"} <= set(tasks.celery_app.tasks)


def test_watchdog_task_requeues_a_stale_job_and_enqueues_it(
    tasks: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    sim: SimulatorThread,
) -> None:
    from datetime import UTC, datetime, timedelta

    from measurehub.db.models import Job

    job_id = make_job(session_factory, sim.port, status=JobStatus.RUNNING)
    with session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
        session.commit()
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(tasks.run_job, "delay", lambda *args: sent.append(args))

    assert tasks.watchdog.run() == {"requeued": 1, "failed": 0, "cancelled": 0}

    assert sent == [(str(job_id),)]
    assert load(session_factory, job_id).status is JobStatus.QUEUED


def test_poll_task_reports_instrument_statuses(
    tasks: ModuleType, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    from measurehub.db.models import InstrumentStatus
    from tests.integration.test_watchdog import make_instrument

    instrument_id = make_instrument(session_factory, sim.port, status=InstrumentStatus.OFFLINE)
    assert tasks.poll_instruments_task.run() == {str(instrument_id): "online"}


def test_busy_retries_are_unlimited(tasks: ModuleType) -> None:
    """Celery treats retry(max_retries=None) as "task default" (3), so the limit is on the task."""
    assert tasks.run_job.max_retries is None
    assert tasks.run_job.default_retry_delay is not None
