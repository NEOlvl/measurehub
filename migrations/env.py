"""Alembic environment: the database URL comes from MH_DATABASE_URL (sync driver)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from measurehub.core.settings import get_settings
from measurehub.db import models  # noqa: F401  (registers tables on the metadata)
from measurehub.db.base import Base

target_metadata = Base.metadata


def _url() -> str:
    return context.config.get_main_option("sqlalchemy.url") or get_settings().sync_database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
