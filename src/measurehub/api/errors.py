"""RFC 9457 problem+json error responses."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"
UNPROCESSABLE = 422
log = structlog.get_logger()


class ApiError(Exception):
    """An error that is rendered as ``application/problem+json``."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        errors: list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors
        self.headers = headers


def not_found(entity: str, key: object) -> ApiError:
    return ApiError(404, "Not Found", f"{entity} {key} does not exist")


def conflict(detail: str) -> ApiError:
    return ApiError(409, "Conflict", detail)


def unprocessable(detail: str, errors: list[Any] | None = None) -> ApiError:
    return ApiError(UNPROCESSABLE, "Unprocessable Content", detail, errors=errors)


def _problem(
    request: Request,
    status: int,
    title: str,
    detail: str | None,
    errors: list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "instance": request.url.path,
    }
    if detail is not None:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _problem(request, exc.status, exc.title, exc.detail, exc.errors, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(request, exc.status_code, _title(exc.status_code), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        for error in errors:
            error.pop("url", None)
        return _problem(
            request,
            UNPROCESSABLE,
            "Unprocessable Content",
            "Invalid request",
            errors,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", path=request.url.path, exc_info=exc)
        return _problem(request, 500, "Internal Server Error", None)


def _title(status: int) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"
