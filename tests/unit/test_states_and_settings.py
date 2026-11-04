import pytest

from measurehub.core.settings import Settings
from measurehub.core.states import ALLOWED_TRANSITIONS, JobStatus, can_transition

# Transitions of the state diagram in the specification (section 5.4).
DIAGRAM = {
    ("queued", "running"),
    ("queued", "cancelled"),
    ("running", "processing"),
    ("running", "cancelling"),
    ("running", "queued"),
    ("running", "failed"),
    ("cancelling", "cancelled"),
    ("processing", "completed"),
    ("processing", "failed"),
    ("completed", "processing"),
}


def test_transition_table_matches_the_diagram() -> None:
    actual = {(a.value, b.value) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
    # the only addition to the diagram: a job whose enqueue failed cannot stay queued forever
    assert actual == DIAGRAM | {("queued", "failed")}


@pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_states_have_no_exits(status: JobStatus) -> None:
    assert not any(can_transition(status, other) for other in JobStatus)


def test_every_status_is_in_the_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://u:p%40ss@db:5432/mh",
            "postgresql+psycopg://u:p%40ss@db:5432/mh",
        ),
        ("sqlite+aiosqlite:///x.db", "sqlite:///x.db"),
        ("postgresql+psycopg://u:p@db/mh", "postgresql+psycopg://u:p@db/mh"),
    ],
)
def test_sync_database_url(url: str, expected: str) -> None:
    settings = Settings(database_url=url, secret_key="0123456789abcdef")
    assert settings.sync_database_url == expected


def test_secret_key_must_be_long_enough() -> None:
    with pytest.raises(ValueError, match="secret_key"):
        Settings(database_url="sqlite://", secret_key="short")


def test_settings_come_from_mh_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MH_DATABASE_URL", "sqlite+aiosqlite:///env.db")
    monkeypatch.setenv("MH_SECRET_KEY", "0123456789abcdef")
    monkeypatch.setenv("MH_INSTRUMENT_LOCK_TTL_S", "7")
    settings = Settings()
    assert settings.database_url == "sqlite+aiosqlite:///env.db"
    assert settings.instrument_lock_ttl_s == 7
