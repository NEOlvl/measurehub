"""Alembic migration (apply, drift check, revert) and the create-user CLI command."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from measurehub.cli import main
from measurehub.core.security import verify_password
from measurehub.db.base import Base
from measurehub.db.models import User

ROOT = Path(__file__).resolve().parents[2]
DESC_INDEX = "ix_jobs_created_at_desc"


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migration_matches_models_and_is_reversible(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    config = alembic_config(url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) >= set(Base.metadata.tables)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            # alembic cannot compare expression indexes (created_at DESC) reliably
            opts={
                "include_object": lambda obj, name, type_, reflected, compare_to: name != DESC_INDEX
            },
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == []

    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(config, "head")  # and up again
    engine.dispose()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}"
    monkeypatch.setenv("MH_DATABASE_URL", url)
    monkeypatch.setenv("MH_SECRET_KEY", "0123456789abcdef")
    from measurehub.core import settings

    settings.get_settings.cache_clear()
    command.upgrade(alembic_config(f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"), "head")
    yield url
    settings.get_settings.cache_clear()


def test_create_user_command(cli_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["create-user", "--username", "bob", "--password", "s3cret-pass"]) == 0
    assert "created" in capsys.readouterr().out

    assert main(["create-user", "--username", "bob", "--password", "x"]) == 1
    assert main(["create-user", "--username", "bob", "--password", "x", "--if-missing"]) == 0

    engine = create_engine(cli_env.replace("+aiosqlite", ""))
    with Session(engine) as session:
        user = session.scalars(select(User)).one()
    assert user.username == "bob"
    assert user.password_hash != "s3cret-pass"
    assert verify_password("s3cret-pass", user.password_hash)
    assert not verify_password("wrong", user.password_hash)


def test_create_user_reads_bootstrap_env(cli_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from measurehub.core import settings

    monkeypatch.setenv("MH_BOOTSTRAP_USERNAME", "root")
    monkeypatch.setenv("MH_BOOTSTRAP_PASSWORD", "bootstrap-pass")
    settings.get_settings.cache_clear()
    assert main(["create-user", "--if-missing"]) == 0
    assert main(["create-user", "--if-missing"]) == 0  # idempotent for compose restarts


def test_create_user_needs_credentials(cli_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["create-user"]) == 2
    assert "required" in capsys.readouterr().err
