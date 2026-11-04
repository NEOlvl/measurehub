"""Synchronous HTTP client for the MeasureHub API (httpx).

The client is blocking on purpose: the GUI never calls it directly, every call goes through
:func:`measurehub_client.controller.Controller.run` and executes in a ``QThreadPool`` thread
(MH-48). The access token lives only in memory.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """The server answered with an error (``application/problem+json``)."""

    def __init__(
        self, status: int, title: str, detail: str | None = None, errors: list[Any] | None = None
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors or []

    def message(self) -> str:
        """Text for the user: the detail, plus the offending fields of a 422."""
        parts = [self.detail or self.title]
        for error in self.errors:
            location = ".".join(str(p) for p in error.get("loc", []) if p != "body")
            parts.append(f"{location}: {error.get('msg', '')}".strip(": "))
        return "\n".join(parts)


class SessionExpiredError(ApiError):
    """The access token was rejected (expired or invalid): the user has to log in again."""


class ConnectionFailedError(Exception):
    """The server could not be reached."""


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=f"{self.base_url}/api/v1", timeout=timeout, transport=transport
        )
        self._token: str | None = None

    # -- session -----------------------------------------------------------------------------

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    def login(self, username: str, password: str) -> None:
        response = self._send(
            "POST", "/auth/login", json={"username": username, "password": password}
        )
        self._token = response.json()["access_token"]

    def logout(self) -> None:
        self._token = None

    def ws_url(self, path: str) -> str:
        """``ws://host/ws/...?token=...`` for the current session."""
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}{path}?token={self._token or ''}"

    # -- transport ---------------------------------------------------------------------------

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise ConnectionFailedError(
                f"Нет связи с сервером {self.base_url}: {type(exc).__name__}"
            ) from exc
        if response.is_success:
            return response
        try:
            body = response.json()
        except ValueError:
            body = {}
        error_cls = (
            SessionExpiredError
            if (response.status_code == 401 and path != "/auth/login")
            else ApiError
        )
        raise error_cls(
            response.status_code,
            body.get("title", response.reason_phrase),
            body.get("detail"),
            body.get("errors"),
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._send(method, path, **kwargs).json()

    # -- resources ---------------------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return dict(self._json("GET", "/auth/me"))

    def list_instruments(self) -> list[dict[str, Any]]:
        return list(self._json("GET", "/instruments", params={"limit": 200})["items"])

    def check_instrument(self, instrument_id: str) -> dict[str, Any]:
        return dict(self._json("POST", f"/instruments/{instrument_id}/check"))

    def list_methods(self) -> list[dict[str, Any]]:
        return list(self._json("GET", "/methods", params={"limit": 200})["items"])

    def list_samples(self) -> list[dict[str, Any]]:
        return list(self._json("GET", "/samples", params={"limit": 200})["items"])

    def measurement_schema(self, measurement_type: str = "vna_sweep") -> dict[str, Any]:
        return dict(self._json("GET", f"/measurement-types/{measurement_type}/schema"))

    def list_jobs(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return dict(self._json("GET", "/jobs", params=params))

    def create_job(
        self,
        method_id: str,
        instrument_id: str,
        sample_id: str | None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "method_id": method_id,
            "instrument_id": instrument_id,
            "sample_id": sample_id,
            "params": params or None,
        }
        return dict(self._json("POST", "/jobs", json=body))

    def get_job(self, job_id: str) -> dict[str, Any]:
        return dict(self._json("GET", f"/jobs/{job_id}"))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return dict(self._json("POST", f"/jobs/{job_id}/cancel"))

    def retry_job(self, job_id: str) -> dict[str, Any]:
        return dict(self._json("POST", f"/jobs/{job_id}/retry"))

    def job_points(self, job_id: str) -> list[dict[str, Any]]:
        page = self._json("GET", f"/jobs/{job_id}/points", params={"limit": 20000})
        return list(page["items"])

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._json("GET", f"/jobs/{job_id}/events", params={"limit": 200})["items"])

    def job_result(self, job_id: str) -> dict[str, Any]:
        return dict(self._json("GET", f"/jobs/{job_id}/result"))

    def export_job(self, job_id: str, fmt: str = "csv", kind: str = "result") -> bytes:
        response = self._send("GET", f"/jobs/{job_id}/export", params={"format": fmt, "kind": kind})
        return response.content
