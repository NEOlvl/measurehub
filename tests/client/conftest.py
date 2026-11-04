"""Fixtures for the GUI tests: a real server (uvicorn) with a simulator and a worker thread.

The client under test talks to the server over real HTTP and real WebSockets; only the Celery
queue is replaced by a thread that runs the job engine (the same code the worker runs).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
import uvicorn
from PyQt6.QtWidgets import QMessageBox

from measurehub.api.app import create_app
from measurehub.core.settings import Settings
from measurehub.worker.engine import EngineContext, execute_job
from measurehub_client.api import ApiClient
from measurehub_client.controller import Controller
from simulator.scpi import ScpiEngine
from simulator.server import SimulatorThread
from tests.conftest import PASSWORD, SIM_SAMPLE

# 0.25 s per segment: a 100-point job takes about a second, long enough to watch it grow
SLOW_METHOD = {"points": 100, "segment_size": 25, "ifbw_hz": 100.0}


class LiveServer:
    """The API on a real port; can be stopped and started again on the same port."""

    def __init__(
        self, settings: Settings, ctx: EngineContext, redis_server: fakeredis.FakeServer
    ) -> None:
        self._settings = settings
        self._ctx = ctx
        self._redis_server = redis_server
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.jobs: list[threading.Thread] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _dispatch(self, job_id: uuid.UUID) -> str:
        thread = threading.Thread(target=execute_job, args=(job_id, self._ctx), daemon=True)
        thread.start()
        self.jobs.append(thread)
        return "thread"

    def start(self) -> None:
        app = create_app(
            self._settings,
            redis=fakeredis.aioredis.FakeRedis(server=self._redis_server),
            dispatcher=self._dispatch,
        )
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", ws="websockets-sansio"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._server is not None and self._thread is not None:
            self._server.should_exit = True
            self._thread.join(timeout=15)
            self._server = None

    def wait_for_jobs(self) -> None:
        for thread in self.jobs:
            thread.join(timeout=30)


@pytest.fixture
def live_server(
    settings: Settings,
    sync_engine: object,
    engine_ctx: EngineContext,
    redis_server: fakeredis.FakeServer,
    user_id: uuid.UUID,
) -> Iterator[LiveServer]:
    server = LiveServer(settings, engine_ctx, redis_server)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def slow_sim() -> Iterator[SimulatorThread]:
    engine = ScpiEngine(SIM_SAMPLE, noise_k=0.0, time_scale=1.0)
    with SimulatorThread(engine) as thread:
        yield thread


@dataclass
class Seed:
    instrument_id: str
    method_id: str
    sample_id: str


def seed_server(base_url: str, sim_port: int) -> Seed:
    """Create what the engineer would create through the API: instrument, method, sample."""
    with httpx.Client(base_url=f"{base_url}/api/v1", timeout=30) as http:
        token = http.post("/auth/login", json={"username": "alice", "password": PASSWORD})
        http.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        instrument = http.post(
            "/instruments",
            json={
                "name": "VNA-1",
                "resource": f"TCPIP0::127.0.0.1::{sim_port}::SOCKET",
                "driver": "sim_vna",
            },
        ).json()
        method = http.post("/methods", json={"name": "NRW-100", "params": SLOW_METHOD}).json()
        sample = http.post(
            "/samples", json={"code": "F-001", "material": "феррит", "thickness_mm": 3.0}
        ).json()
    return Seed(instrument["id"], method["id"], sample["id"])


@pytest.fixture
def seed(live_server: LiveServer, slow_sim: SimulatorThread) -> Seed:
    return seed_server(live_server.url, slow_sim.port)


@pytest.fixture
def controller(qapp: object, live_server: LiveServer) -> Iterator[Controller]:
    ctl = Controller(ApiClient(live_server.url))
    yield ctl
    ctl.shutdown()


@pytest.fixture
def logged_in(controller: Controller) -> Controller:
    controller.api.login("alice", PASSWORD)
    return controller


@pytest.fixture
def errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Message boxes would block a test: record them instead."""
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda parent, title, text: shown.append(text))
    )
    return shown


Wait = Callable[[Callable[[], bool], int], None]
