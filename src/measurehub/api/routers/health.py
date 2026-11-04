"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with request.app.state.sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "unavailable", "checks": checks},
        status_code=200 if healthy else 503,
    )
