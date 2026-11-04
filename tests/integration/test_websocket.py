"""WebSocket channels, driven through Starlette's TestClient (a real ASGI WebSocket session)."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from measurehub.worker.engine import EngineContext, execute_job
from simulator.scpi import ScpiEngine
from simulator.server import SimulatorThread
from tests.conftest import PASSWORD, SIM_SAMPLE

API = "/api/v1"
SLOW_SWEEP = {"points": 100, "segment_size": 25, "ifbw_hz": 100.0}  # 0.25 s per segment


@pytest.fixture
def slow_sim() -> Iterator[SimulatorThread]:
    engine = ScpiEngine(SIM_SAMPLE, noise_k=0.0, time_scale=1.0)
    with SimulatorThread(engine) as thread:
        yield thread


@pytest.fixture
def web(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        token = client.post(
            f"{API}/auth/login", json={"username": "alice", "password": PASSWORD}
        ).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        client.token = token
        yield client


def create_job(web: TestClient, app: FastAPI, port: int) -> dict[str, Any]:
    """A queued job (nothing runs it until the test says so)."""
    instrument = web.post(
        f"{API}/instruments",
        json={"name": "sim", "resource": f"TCPIP0::127.0.0.1::{port}::SOCKET", "driver": "sim_vna"},
    ).json()
    method = web.post(f"{API}/methods", json={"name": "m", "params": SLOW_SWEEP}).json()
    sample = web.post(
        f"{API}/samples", json={"code": "S", "material": "x", "thickness_mm": 3.0}
    ).json()
    app.state.dispatcher = lambda job_id: "task"
    job: dict[str, Any] = web.post(
        f"{API}/jobs",
        json={
            "instrument_id": instrument["id"],
            "method_id": method["id"],
            "sample_id": sample["id"],
        },
    ).json()
    return job


def run_in_background(job_id: str, ctx: EngineContext) -> threading.Thread:
    thread = threading.Thread(target=execute_job, args=(uuid.UUID(job_id), ctx), daemon=True)
    thread.start()
    return thread


def read_until(ws: WebSocketTestSession, status: str) -> list[dict[str, Any]]:
    """Messages up to and including the status event ``status``."""
    messages = []
    while True:
        message: dict[str, Any] = ws.receive_json()
        messages.append(message)
        if message["type"] == "status" and message["status"] == status:
            return messages


def url(web: TestClient, path: str) -> str:
    return f"{path}?token={web.token}"


def test_job_channel_sends_a_snapshot_first_then_the_live_events(
    web: TestClient, app: FastAPI, engine_ctx: EngineContext, slow_sim: SimulatorThread
) -> None:
    job = create_job(web, app, slow_sim.port)
    with web.websocket_connect(url(web, f"/ws/jobs/{job['id']}")) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert (snapshot["job_id"], snapshot["status"]) == (job["id"], "queued")
        assert (snapshot["points_done"], snapshot["points_total"]) == (0, 100)

        run_in_background(job["id"], engine_ctx)
        messages = read_until(ws, "completed")

    kinds = [m["type"] for m in messages]
    assert kinds[0] == "status"
    assert messages[0]["status"] == "running"
    assert kinds.count("segment") == 4
    assert kinds.count("progress") == 4
    progress = [m["points_done"] for m in messages if m["type"] == "progress"]
    assert progress == [25, 50, 75, 100]
    assert [m["status"] for m in messages if m["type"] == "status"] == [
        "running",
        "processing",
        "completed",
    ]
    assert len([f for m in messages if m["type"] == "segment" for f in m["freq_hz"]]) == 100


def test_event_latency_is_below_one_second(
    web: TestClient, app: FastAPI, engine_ctx: EngineContext, slow_sim: SimulatorThread
) -> None:
    """NFR-03: from the worker's event timestamp to the client."""
    job = create_job(web, app, slow_sim.port)
    delays = []
    with web.websocket_connect(url(web, f"/ws/jobs/{job['id']}")) as ws:
        ws.receive_json()
        run_in_background(job["id"], engine_ctx)
        while True:
            message = ws.receive_json()
            received = datetime.now(UTC)  # measured on arrival, not after the whole sweep
            sent = datetime.fromisoformat(message["ts"].replace("Z", "+00:00"))
            delays.append((received - sent).total_seconds())
            if message["type"] == "status" and message["status"] == "completed":
                break
    assert delays
    assert max(delays) < 1.0, delays


def test_client_joining_in_the_middle_gets_the_current_state_then_the_rest(
    web: TestClient, app: FastAPI, engine_ctx: EngineContext, slow_sim: SimulatorThread
) -> None:
    job = create_job(web, app, slow_sim.port)
    worker = run_in_background(job["id"], engine_ctx)
    deadline = time.monotonic() + 30
    while web.get(f"{API}/jobs/{job['id']}").json()["points_done"] < 50:
        assert time.monotonic() < deadline
        time.sleep(0.05)

    with web.websocket_connect(url(web, f"/ws/jobs/{job['id']}")) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["status"] in {"running", "processing", "completed"}
        assert snapshot["points_done"] >= 50
        last_progress = snapshot["points_done"]
        if snapshot["status"] != "completed":
            for message in read_until(ws, "completed"):
                if message["type"] == "progress":
                    assert message["points_done"] >= last_progress
                    last_progress = message["points_done"]
    worker.join(timeout=10)
    assert last_progress == 100


def test_global_channel_snapshot_and_status_changes(
    web: TestClient, app: FastAPI, engine_ctx: EngineContext, slow_sim: SimulatorThread
) -> None:
    job = create_job(web, app, slow_sim.port)
    with web.websocket_connect(url(web, "/ws/events")) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert [i["status"] for i in snapshot["instruments"]] == ["offline"]
        assert [(j["job_id"], j["status"]) for j in snapshot["jobs"]] == [(job["id"], "queued")]

        run_in_background(job["id"], engine_ctx)
        seen: list[tuple[str, str]] = []
        while seen[-1:] != [("status", "completed")]:
            message = ws.receive_json()
            seen.append((message["type"], message["status"]))
    assert ("status", "running") in seen
    assert ("instrument", "busy") in seen
    assert ("status", "completed") in seen
    assert seen.index(("status", "running")) < seen.index(("status", "processing"))
    assert all(kind in {"status", "instrument"} for kind, _ in seen)  # no segment data here


@pytest.mark.parametrize("token", ["", "garbage"])
def test_bad_token_is_closed_with_4401(web: TestClient, token: str) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        web.websocket_connect(f"/ws/events?token={token}") as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4401


def test_unknown_job_is_closed_with_4404(web: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        web.websocket_connect(url(web, f"/ws/jobs/{uuid.uuid4()}")) as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4404


def test_disconnecting_clients_do_not_break_later_connections(
    web: TestClient, app: FastAPI, slow_sim: SimulatorThread
) -> None:
    job = create_job(web, app, slow_sim.port)
    for _ in range(3):
        with web.websocket_connect(url(web, f"/ws/jobs/{job['id']}")) as ws:
            assert ws.receive_json()["type"] == "snapshot"
    with web.websocket_connect(url(web, f"/ws/jobs/{job['id']}")) as ws:
        assert ws.receive_json()["status"] == "queued"
