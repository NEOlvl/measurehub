"""Shared fixtures: SQLite database, fake Redis, simulator, API client with inline job execution."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from measurehub.api.app import create_app
from measurehub.core.security import hash_password
from measurehub.core.settings import Settings
from measurehub.db import models
from measurehub.db.base import Base
from measurehub.drivers.registry import create_driver
from measurehub.worker.engine import EngineContext, execute_job
from measurehub.worker.events import RedisEventSink
from simulator.model import Sample
from simulator.scpi import ScpiEngine
from simulator.server import SimulatorThread

PASSWORD = "correct horse battery"
SIM_SAMPLE = Sample.from_parts(10, 0.5, 1.5, 0.2, 0.003)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        redis_url="redis://unused:6379/0",
        secret_key="test-secret-key-0123456789abcdef",
        visa_timeout_ms=500,
        log_level="WARNING",
    )


@pytest.fixture
def sync_engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine(settings.sync_database_url)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(sync_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(sync_engine, expire_on_commit=False)


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def sim() -> Iterator[SimulatorThread]:
    engine = ScpiEngine(SIM_SAMPLE, seed=11, time_scale=0.0)
    with SimulatorThread(engine) as thread:
        yield thread


@pytest.fixture
def sleeps() -> list[float]:
    """Backoff delays requested by the engine (nothing really sleeps in tests)."""
    return []


@pytest.fixture
def engine_ctx(
    session_factory: sessionmaker[Session],
    redis_server: fakeredis.FakeServer,
    sleeps: list[float],
) -> EngineContext:
    redis = fakeredis.FakeRedis(server=redis_server)
    return EngineContext(
        session_factory=session_factory,
        redis=redis,
        driver_factory=lambda key, resource: create_driver(
            key, resource, timeout_ms=500, connect_attempts=1
        ),
        lock_ttl_s=30,
        sleep=sleeps.append,
        events=RedisEventSink(redis),
    )


@pytest.fixture
def user_id(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        user = models.User(username="alice", password_hash=hash_password(PASSWORD))
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def app(
    settings: Settings,
    sync_engine: Engine,
    engine_ctx: EngineContext,
    redis_server: fakeredis.FakeServer,
    user_id: uuid.UUID,
) -> FastAPI:
    """API app; ``POST /jobs`` runs the job synchronously via the engine (no Celery)."""

    def inline_dispatch(job_id: uuid.UUID) -> str:
        execute_job(job_id, engine_ctx)
        return "inline"

    return create_app(
        settings,
        redis=fakeredis.aioredis.FakeRedis(server=redis_server),
        dispatcher=inline_dispatch,
    )


def anonymous_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Client logged in as ``alice``."""
    async with anonymous_client(app) as http:
        response = await http.post(
            "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield http


async def make_fixtures(
    http: httpx.AsyncClient,
    sim_port: int,
    *,
    processor: str = "nrw",
    method_params: dict[str, int | float] | None = None,
) -> dict[str, str]:
    """Create an instrument pointing at the simulator, a method and a 3 mm sample."""
    instrument = await http.post(
        "/api/v1/instruments",
        json={
            "name": "sim-1",
            "resource": f"TCPIP0::127.0.0.1::{sim_port}::SOCKET",
            "driver": "sim_vna",
        },
    )
    method = await http.post(
        "/api/v1/methods",
        json={
            "name": "quick",
            "processor": processor,
            "params": method_params or {"points": 201, "segment_size": 50},
        },
    )
    sample = await http.post(
        "/api/v1/samples", json={"code": "S-1", "material": "ferrite", "thickness_mm": 3.0}
    )
    for response in (instrument, method, sample):
        assert response.status_code == 201, response.text
    return {
        "instrument_id": instrument.json()["id"],
        "method_id": method.json()["id"],
        "sample_id": sample.json()["id"],
    }
