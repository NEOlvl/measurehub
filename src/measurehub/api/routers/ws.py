"""WebSocket channels: ``/ws/jobs/{id}`` and ``/ws/events`` (technical specification, 7.2).

Authentication is the access token in the ``token`` query parameter. The first message is
always a ``snapshot`` of the current state, then the live events follow. The subscription is
made *before* the snapshot is read, so no event can fall into the gap between the two.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import jwt
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from measurehub.core.events import GLOBAL_CHANNEL, job_channel, timestamp
from measurehub.core.security import decode_access_token
from measurehub.core.states import UNFINISHED
from measurehub.db.models import Instrument, Job, User

router = APIRouter(tags=["events"])

CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404

Snapshot = Callable[[FastAPI], Awaitable[dict[str, Any] | None]]


async def _authenticate(app: FastAPI, token: str) -> User | None:
    try:
        subject = decode_access_token(token, app.state.settings.secret_key)
        async with app.state.sessionmaker() as session:
            user = await session.get(User, uuid.UUID(subject))
    except (jwt.PyJWTError, ValueError):
        return None
    return user if user is not None and user.is_active else None


def _job_snapshot(job: Job) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "job_id": str(job.id),
        "status": job.status.value,
        "points_done": job.points_done,
        "points_total": job.points_total,
        "segments_done": job.segments_done,
        "segments_total": job.segments_total,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "ts": timestamp(),
    }


async def _forward(websocket: WebSocket, pubsub: Any) -> None:
    """Send Redis messages to the client until either side goes away."""

    async def pump() -> None:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
            if message is not None:
                data = message["data"]
                await websocket.send_text(data.decode() if isinstance(data, bytes) else data)

    async def watch() -> None:
        while True:
            if (await websocket.receive())["type"] == "websocket.disconnect":
                return

    tasks = {asyncio.ensure_future(pump()), asyncio.ensure_future(watch())}
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _serve(websocket: WebSocket, token: str, channel: str, snapshot: Snapshot) -> None:
    """Common flow: accept, authenticate, subscribe, send the snapshot, forward events."""
    await websocket.accept()
    app = websocket.app
    if await _authenticate(app, token) is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return
    pubsub = app.state.redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        first = await snapshot(app)
        if first is None:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return
        await websocket.send_json(first)
        await _forward(websocket, pubsub)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


@router.websocket("/ws/jobs/{job_id}")
async def job_events(websocket: WebSocket, job_id: uuid.UUID, token: str = "") -> None:
    async def snapshot(app: FastAPI) -> dict[str, Any] | None:
        async with app.state.sessionmaker() as session:
            job = await session.get(Job, job_id)
        return _job_snapshot(job) if job is not None else None

    await _serve(websocket, token, job_channel(job_id), snapshot)


@router.websocket("/ws/events")
async def global_events(websocket: WebSocket, token: str = "") -> None:
    async def snapshot(app: FastAPI) -> dict[str, Any] | None:
        async with app.state.sessionmaker() as session:
            instruments = (await session.execute(select(Instrument.id, Instrument.status))).all()
            jobs = (
                await session.execute(
                    select(Job.id, Job.status, Job.points_done, Job.points_total).where(
                        Job.status.in_(UNFINISHED)
                    )
                )
            ).all()
        return {
            "type": "snapshot",
            "instruments": [
                {"instrument_id": str(i), "status": status.value} for i, status in instruments
            ],
            "jobs": [
                {
                    "job_id": str(i),
                    "status": status.value,
                    "points_done": done,
                    "points_total": total,
                }
                for i, status, done, total in jobs
            ],
            "ts": timestamp(),
        }

    await _serve(websocket, token, GLOBAL_CHANNEL, snapshot)
