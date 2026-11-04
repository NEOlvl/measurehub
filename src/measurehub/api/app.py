"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from redis.asyncio import Redis

from measurehub import __version__
from measurehub.api.errors import install_error_handlers
from measurehub.api.routers import auth, health, instruments, jobs, methods, samples, ws
from measurehub.core.logging import configure_logging
from measurehub.core.settings import Settings, get_settings
from measurehub.db.session import make_async_engine, make_async_sessionmaker
from measurehub.drivers.registry import DriverFactory, create_driver
from measurehub.services.jobs import Dispatcher


def celery_dispatcher(job_id: uuid.UUID) -> str | None:
    """Default dispatcher: enqueue the ``run_job`` Celery task."""
    from measurehub.worker.tasks import run_job

    return str(run_job.delay(str(job_id)).id)


def create_app(
    settings: Settings | None = None,
    *,
    redis: Any = None,
    dispatcher: Dispatcher | None = None,
    driver_factory: DriverFactory | None = None,
) -> FastAPI:
    """Build the app; the keyword arguments exist so tests can inject fakes."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = make_async_engine(settings)
    redis_client = redis if redis is not None else Redis.from_url(settings.redis_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()
        await redis_client.aclose()

    app = FastAPI(title="MeasureHub", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = make_async_sessionmaker(engine)
    app.state.redis = redis_client
    app.state.dispatcher = dispatcher or celery_dispatcher
    app.state.driver_factory = driver_factory or (
        lambda key, resource: create_driver(
            key, resource, timeout_ms=settings.visa_timeout_ms, connect_attempts=1
        )
    )
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(ws.router)
    v1 = APIRouter(prefix="/api/v1")
    for module in (auth, instruments, methods, samples, jobs):
        v1.include_router(module.router)
    app.include_router(v1)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory measurehub.api.app:app_factory``."""
    return create_app()
