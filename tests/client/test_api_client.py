"""The HTTP client: tokens, error mapping, URLs (no server, httpx mock transport)."""

from __future__ import annotations

import json

import httpx
import pytest

from measurehub_client.api import (
    ApiClient,
    ApiError,
    ConnectionFailedError,
    SessionExpiredError,
)


def client_with(**routes: httpx.Response) -> ApiClient:
    def respond(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        return routes[key]

    return ApiClient("http://server:8000", transport=httpx.MockTransport(respond))


def test_login_keeps_the_token_in_memory_and_sends_it() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"id": "1", "username": "alice", "is_active": True})

    api = ApiClient("http://server:8000/", transport=httpx.MockTransport(handler))
    assert not api.logged_in
    api.login("alice", "pw")
    assert api.logged_in
    assert json.loads(seen[0].content) == {"username": "alice", "password": "pw"}
    assert "authorization" not in seen[0].headers
    assert api.me()["username"] == "alice"
    assert seen[1].headers["authorization"] == "Bearer tok"
    assert seen[1].url.path == "/api/v1/auth/me"
    api.logout()
    assert api.token is None


def test_wrong_password_is_an_api_error_not_an_expired_session() -> None:
    api = client_with(
        **{
            "POST /api/v1/auth/login": httpx.Response(
                401,
                json={"title": "Unauthorized", "detail": "Wrong username or password"},
                headers={"content-type": "application/problem+json"},
            )
        }
    )
    with pytest.raises(ApiError) as caught:
        api.login("alice", "bad")
    assert not isinstance(caught.value, SessionExpiredError)
    assert caught.value.status == 401
    assert caught.value.message() == "Wrong username or password"


def test_a_rejected_token_means_the_session_expired() -> None:
    api = client_with(
        **{"GET /api/v1/jobs": httpx.Response(401, json={"title": "Unauthorized", "detail": "x"})}
    )
    with pytest.raises(SessionExpiredError):
        api.list_jobs()


def test_validation_errors_list_the_fields() -> None:
    body = {
        "title": "Unprocessable Content",
        "detail": "Invalid measurement parameters",
        "errors": [{"loc": ["body", "points"], "msg": "Input should be >= 2", "type": "x"}],
    }
    api = client_with(**{"POST /api/v1/jobs": httpx.Response(422, json=body)})
    with pytest.raises(ApiError) as caught:
        api.create_job("m", "i", None)
    assert caught.value.status == 422
    assert caught.value.message().splitlines() == [
        "Invalid measurement parameters",
        "points: Input should be >= 2",
    ]


def test_server_down_is_a_connection_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    api = ApiClient("http://server:8000", transport=httpx.MockTransport(refuse))
    with pytest.raises(ConnectionFailedError, match="http://server:8000"):
        api.list_jobs()


def test_non_json_error_body_still_gives_an_api_error() -> None:
    api = client_with(**{"GET /api/v1/jobs": httpx.Response(502, text="Bad Gateway")})
    with pytest.raises(ApiError) as caught:
        api.list_jobs()
    assert caught.value.status == 502


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://localhost:8000", "ws://localhost:8000/ws/events?token=tok"),
        ("https://mh.example.org", "wss://mh.example.org/ws/events?token=tok"),
        ("http://localhost:8000/", "ws://localhost:8000/ws/events?token=tok"),
    ],
)
def test_websocket_urls(base: str, expected: str) -> None:
    api = ApiClient(
        base,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"access_token": "tok"})),
    )
    api.login("a", "b")
    assert api.ws_url("/ws/events") == expected


def test_create_job_sends_only_the_given_overrides_and_export_returns_bytes() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "j"})
        assert dict(request.url.params) == {"format": "csv", "kind": "raw"}
        return httpx.Response(200, content=b"a,b\n1,2\n")

    api = ApiClient("http://server:8000", transport=httpx.MockTransport(handler))
    api.create_job("m", "i", "s", {"points": 50})
    api.create_job("m", "i", None)
    assert bodies == [
        {"method_id": "m", "instrument_id": "i", "sample_id": "s", "params": {"points": 50}},
        {"method_id": "m", "instrument_id": "i", "sample_id": None, "params": None},
    ]
    assert api.export_job("j", "csv", "raw") == b"a,b\n1,2\n"
