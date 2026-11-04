"""Application settings, read from ``MH_*`` environment variables only."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_SYNC_DRIVERS = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "sqlite+aiosqlite": "sqlite",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MH_", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = Field(min_length=16)
    access_token_minutes: int = Field(default=60, ge=1)
    instrument_lock_ttl_s: int = Field(default=30, ge=1)
    visa_timeout_ms: int = Field(default=5000, ge=100)
    watchdog_interval_s: int = Field(default=30, ge=1)
    watchdog_stale_s: int = Field(default=60, ge=1)
    watchdog_max_requeues: int = Field(default=2, ge=0)
    poll_interval_s: int = Field(default=60, ge=1)
    log_level: str = "INFO"
    bootstrap_username: str | None = None
    bootstrap_password: str | None = None

    @property
    def sync_database_url(self) -> str:
        """URL for the synchronous engine used by workers, Alembic and the CLI."""
        url = make_url(self.database_url)
        url = url.set(drivername=_SYNC_DRIVERS.get(url.drivername, url.drivername))
        return url.render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
