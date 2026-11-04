from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from measurehub.core.settings import Settings
from tests.conftest import PASSWORD, anonymous_client

API = "/api/v1"


async def test_live(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_checks_database_and_redis(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_ready_is_503_when_redis_is_down(app: FastAPI, client: httpx.AsyncClient) -> None:
    class Broken:
        async def ping(self) -> None:
            raise ConnectionError("redis down")

    app.state.redis = Broken()
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["redis"].startswith("error")
    assert response.json()["checks"]["database"] == "ok"


async def test_login_and_me(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert "password_hash" not in response.json()


async def test_login_rejects_wrong_password_and_unknown_user(client: httpx.AsyncClient) -> None:
    for body in (
        {"username": "alice", "password": "nope"},
        {"username": "mallory", "password": PASSWORD},
    ):
        response = await client.post(f"{API}/auth/login", json=body)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


async def test_endpoints_require_a_valid_token(app: FastAPI) -> None:
    async with anonymous_client(app) as anon:
        assert (await anon.get(f"{API}/instruments")).status_code == 401
        garbage = await anon.get(f"{API}/jobs", headers={"Authorization": "Bearer garbage"})
        assert garbage.status_code == 401


async def test_expired_token_is_rejected(client: httpx.AsyncClient, settings: Settings) -> None:
    from measurehub.core.security import create_access_token

    token = create_access_token("00000000-0000-0000-0000-000000000000", settings.secret_key, -1)
    response = await client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_errors_are_problem_json(client: httpx.AsyncClient) -> None:
    missing = await client.get(f"{API}/jobs/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    body = missing.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["instance"] == f"{API}/jobs/00000000-0000-0000-0000-000000000000"
    assert "detail" in body

    invalid = await client.post(f"{API}/samples", json={"code": "", "thickness_mm": -1})
    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/problem+json"
    assert {tuple(e["loc"]) for e in invalid.json()["errors"]} >= {
        ("body", "code"),
        ("body", "material"),
        ("body", "thickness_mm"),
    }

    unknown_route = await client.get("/nope")
    assert unknown_route.status_code == 404
    assert unknown_route.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (201, 0), (10, -1)])
async def test_paging_bounds_are_validated(
    client: httpx.AsyncClient, limit: int, offset: int
) -> None:
    response = await client.get(f"{API}/samples", params={"limit": limit, "offset": offset})
    assert response.status_code == 422
