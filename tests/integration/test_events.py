"""Events published by the engine, the persistent job log and the points endpoint."""

from __future__ import annotations

import json
import math
from typing import Any

import fakeredis
import httpx
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.events import GLOBAL_CHANNEL, job_channel
from measurehub.core.states import JobStatus
from measurehub.db.models import JobEvent, MeasurementPoint
from measurehub.drivers.errors import InstrumentError
from measurehub.drivers.models import VnaSweepParams
from measurehub.worker.cancel import cancel_key
from measurehub.worker.engine import EngineContext, execute_job
from measurehub.worker.events import RedisEventSink
from simulator.server import SimulatorThread
from tests.conftest import make_fixtures
from tests.integration.test_engine import load, make_job
from tests.integration.test_engine_reliability import with_failures

API = "/api/v1"
SWEEP = VnaSweepParams(points=100, segment_size=25, max_retries=3)


def drain(pubsub: Any) -> list[dict[str, Any]]:
    """All messages waiting on a sync fakeredis pubsub."""
    messages = []
    while (message := pubsub.get_message(timeout=0.01)) is not None:
        if message["type"] == "message":
            messages.append(json.loads(message["data"]))
    return messages


def subscribe(ctx: EngineContext, *channels: str) -> Any:
    pubsub = ctx.redis.pubsub()
    pubsub.subscribe(*channels)
    pubsub.get_message(timeout=0.1)  # the subscription confirmation
    return pubsub


def test_engine_publishes_status_segments_and_progress(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    pubsub = subscribe(engine_ctx, job_channel(job_id))

    assert execute_job(job_id, engine_ctx) is JobStatus.COMPLETED

    events = drain(pubsub)
    kinds = [(e["type"], e.get("status")) for e in events]
    assert kinds == [
        ("status", "running"),
        *[("segment", None), ("progress", None)] * 4,
        ("status", "processing"),
        ("status", "completed"),
    ]
    assert all(e["job_id"] == str(job_id) and e["ts"].endswith("Z") for e in events)

    segments = [e for e in events if e["type"] == "segment"]
    assert [s["from_idx"] for s in segments] == [0, 25, 50, 75]
    progress = [e for e in events if e["type"] == "progress"]
    assert [p["points_done"] for p in progress] == [25, 50, 75, 100]
    assert progress[-1]["points_total"] == 100
    assert (progress[-1]["segments_done"], progress[-1]["segments_total"]) == (4, 4)

    # the graph data in the events is the measured data
    with session_factory() as session:
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
    table = np.array(rows)
    assert np.allclose([f for s in segments for f in s["freq_hz"]], table[:, 0])
    s11_db = 20 * np.log10(np.hypot(table[:, 1], table[:, 2]))
    s21_db = 20 * np.log10(np.hypot(table[:, 3], table[:, 4]))
    assert np.allclose([v for s in segments for v in s["s11_db"]], s11_db)
    assert np.allclose([v for s in segments for v in s["s21_db"]], s21_db)


def test_global_channel_carries_status_and_instrument_changes_only(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    pubsub = subscribe(engine_ctx, GLOBAL_CHANNEL)

    execute_job(job_id, engine_ctx)

    events = drain(pubsub)
    assert [e.get("status") for e in events if e["type"] == "status"] == [
        "running",
        "processing",
        "completed",
    ]
    assert [e["status"] for e in events if e["type"] == "instrument"] == ["busy", "online"]
    assert {e["type"] for e in events} == {"status", "instrument"}  # no segment data


def test_retries_and_errors_are_logged_and_published(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    job_id = make_job(session_factory, sim.port, sweep=SWEEP.model_copy(update={"max_retries": 1}))
    failures = {1: [InstrumentError(-230, "stale")] * 5}
    ctx = with_failures(engine_ctx, failures, [])
    pubsub = subscribe(ctx, job_channel(job_id))

    assert execute_job(job_id, ctx) is JobStatus.FAILED

    events = drain(pubsub)
    logs = [e for e in events if e["type"] == "log"]
    assert len(logs) == 1
    assert logs[0]["level"] == "warning"
    assert "Повтор сегмента 1, попытка 1 из 1" in logs[0]["message"]
    error = [e for e in events if e["type"] == "error"]
    assert [(e["code"]) for e in error] == ["instrument_error"]

    with session_factory() as session:
        rows = session.scalars(
            select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
        ).all()
    assert [(r.event, r.level) for r in rows] == [
        ("status", "info"),  # running
        ("log", "warning"),
        ("error", "error"),
        ("status", "error"),  # failed
    ]
    assert rows[-1].payload == {"status": "failed", "from": "running"}


def test_resume_and_cancel_are_logged(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    from tests.integration.test_engine_reliability import save_segments

    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    save_segments(engine_ctx, session_factory, sim, job_id, [0])
    engine_ctx.redis.set(cancel_key(job_id), "1")
    pubsub = subscribe(engine_ctx, job_channel(job_id))

    assert execute_job(job_id, engine_ctx) is JobStatus.CANCELLED

    events = drain(pubsub)
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert statuses == ["running", "cancelling", "cancelled"]
    logs = [e["message"] for e in events if e["type"] == "log"]
    assert logs == ["Продолжение измерения: уже сохранено 25 точек (1 сегментов)"]


def test_a_broken_event_bus_does_not_break_the_measurement(
    engine_ctx: EngineContext, session_factory: sessionmaker[Session], sim: SimulatorThread
) -> None:
    class BrokenRedis:
        def publish(self, *args: object) -> None:
            raise ConnectionError("redis is down")

    from dataclasses import replace

    ctx = replace(engine_ctx, events=RedisEventSink(BrokenRedis()))
    job_id = make_job(session_factory, sim.port, sweep=SWEEP)
    assert execute_job(job_id, ctx) is JobStatus.COMPLETED
    assert load(session_factory, job_id).points_done == 100


# -- HTTP: job log and points ----------------------------------------------------------------------


async def test_job_events_endpoint_returns_the_log_in_order(
    client: httpx.AsyncClient, sim: SimulatorThread
) -> None:
    ids = await make_fixtures(client, sim.port)
    job = (await client.post(f"{API}/jobs", json=ids)).json()

    page = (await client.get(f"{API}/jobs/{job['id']}/events")).json()

    assert [e["payload"]["status"] for e in page["items"]] == [
        "queued",
        "running",
        "processing",
        "completed",
    ]
    assert page["total"] == 4
    assert page["items"][0]["payload"].get("from") is None
    assert all(e["event"] == "status" and e["level"] == "info" for e in page["items"])
    assert [e["id"] for e in page["items"]] == sorted(e["id"] for e in page["items"])
    second = (
        await client.get(f"{API}/jobs/{job['id']}/events", params={"limit": 2, "offset": 2})
    ).json()
    assert [e["payload"]["status"] for e in second["items"]] == ["processing", "completed"]
    missing = await client.get(f"{API}/jobs/00000000-0000-0000-0000-000000000000/events")
    assert missing.status_code == 404


async def test_cancel_and_failure_appear_in_the_job_log(
    client: httpx.AsyncClient, sim: SimulatorThread
) -> None:
    ids = await make_fixtures(client, 1)  # nothing listens on port 1: the job fails
    job = (await client.post(f"{API}/jobs", json=ids)).json()

    items = (await client.get(f"{API}/jobs/{job['id']}/events")).json()["items"]

    assert [e["event"] for e in items][-2:] == ["error", "status"]
    assert items[-2]["payload"]["code"] == "instrument_connection"
    assert items[-1]["level"] == "error"
    assert items[-1]["payload"] == {"status": "failed", "from": "running"}


async def test_points_endpoint(client: httpx.AsyncClient, sim: SimulatorThread) -> None:
    ids = await make_fixtures(client, sim.port)
    job = (await client.post(f"{API}/jobs", json=ids)).json()

    everything = (
        await client.get(f"{API}/jobs/{job['id']}/points", params={"limit": 20000})
    ).json()
    assert everything["total"] == 201
    assert [p["idx"] for p in everything["items"]] == list(range(201))
    first = everything["items"][0]
    assert first["freq_hz"] == pytest.approx(1e9)
    assert math.isfinite(first["s21_re"])
    assert math.isfinite(first["s21_im"])

    page = (
        await client.get(f"{API}/jobs/{job['id']}/points", params={"limit": 50, "offset": 100})
    ).json()
    assert [p["idx"] for p in page["items"]] == list(range(100, 150))
    assert (
        await client.get(f"{API}/jobs/{job['id']}/points", params={"limit": 20001})
    ).status_code == 422
    missing = await client.get(f"{API}/jobs/00000000-0000-0000-0000-000000000000/points")
    assert missing.status_code == 404


async def test_creating_and_cancelling_publish_status_events(
    client: httpx.AsyncClient, sim: SimulatorThread, app: Any, redis_server: fakeredis.FakeServer
) -> None:
    ids = await make_fixtures(client, sim.port)
    app.state.dispatcher = lambda job_id: "task"
    watcher: Any = fakeredis.FakeRedis(server=redis_server)
    pubsub = watcher.pubsub()
    pubsub.subscribe(GLOBAL_CHANNEL)
    pubsub.get_message(timeout=0.1)

    job = (await client.post(f"{API}/jobs", json=ids)).json()
    await client.post(f"{API}/jobs/{job['id']}/cancel")

    events = drain(pubsub)
    assert [(e["job_id"], e["status"]) for e in events] == [
        (job["id"], "queued"),
        (job["id"], "cancelled"),
    ]
