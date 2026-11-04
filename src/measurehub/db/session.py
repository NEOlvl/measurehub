"""Engines and session factories (async for the API, sync for workers and the CLI)."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.settings import Settings


def make_async_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def make_async_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def make_sync_engine(settings: Settings) -> Engine:
    return create_engine(settings.sync_database_url, pool_pre_ping=True)


def make_sync_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
