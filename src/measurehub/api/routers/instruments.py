"""Instruments: CRUD and connectivity check."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import anyio.to_thread
from fastapi import APIRouter, Request
from sqlalchemy import func, select

from measurehub.api.deps import (
    CurrentUser,
    PagingParams,
    Session,
    commit_or_conflict,
    get_or_404,
    paginate,
)
from measurehub.api.errors import conflict, unprocessable
from measurehub.core.states import UNFINISHED
from measurehub.db.models import Instrument, InstrumentStatus, Job
from measurehub.drivers import registry
from measurehub.schemas.api import InstrumentCreate, InstrumentOut, InstrumentUpdate, Page
from measurehub.services.instruments import probe

router = APIRouter(tags=["instruments"])


def _check_driver(key: str) -> None:
    if key not in registry.available():
        raise unprocessable(f"unknown driver {key!r}; available: {registry.available()}")


@router.get("/drivers")
async def list_drivers(user: CurrentUser) -> list[str]:
    return registry.available()


@router.get("/instruments")
async def list_instruments(
    session: Session, user: CurrentUser, paging: PagingParams
) -> Page[InstrumentOut]:
    stmt = select(Instrument).order_by(Instrument.name)
    return await paginate(session, stmt, paging, InstrumentOut)


@router.post("/instruments", status_code=201)
async def create_instrument(
    body: InstrumentCreate, session: Session, user: CurrentUser
) -> InstrumentOut:
    _check_driver(body.driver)
    instrument = Instrument(**body.model_dump())
    session.add(instrument)
    await commit_or_conflict(session, f"Instrument {body.name!r} already exists")
    return InstrumentOut.model_validate(instrument)


@router.get("/instruments/{instrument_id}")
async def get_instrument(
    instrument_id: uuid.UUID, session: Session, user: CurrentUser
) -> InstrumentOut:
    return InstrumentOut.model_validate(
        await get_or_404(session, Instrument, instrument_id, "Instrument")
    )


@router.patch("/instruments/{instrument_id}")
async def update_instrument(
    instrument_id: uuid.UUID, body: InstrumentUpdate, session: Session, user: CurrentUser
) -> InstrumentOut:
    instrument = await get_or_404(session, Instrument, instrument_id, "Instrument")
    changes = body.model_dump(exclude_unset=True)
    if "driver" in changes:
        _check_driver(changes["driver"])
    for field, value in changes.items():
        setattr(instrument, field, value)
    await commit_or_conflict(session, "Instrument name already exists")
    return InstrumentOut.model_validate(instrument)


@router.delete("/instruments/{instrument_id}", status_code=204)
async def delete_instrument(instrument_id: uuid.UUID, session: Session, user: CurrentUser) -> None:
    instrument = await get_or_404(session, Instrument, instrument_id, "Instrument")
    unfinished = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.instrument_id == instrument_id, Job.status.in_(UNFINISHED))
    )
    if unfinished:
        raise conflict("Instrument has unfinished jobs")
    await session.delete(instrument)
    await commit_or_conflict(session, "Instrument is referenced by existing jobs")


@router.post("/instruments/{instrument_id}/check")
async def check_instrument(
    instrument_id: uuid.UUID, session: Session, user: CurrentUser, request: Request
) -> InstrumentOut:
    instrument = await get_or_404(session, Instrument, instrument_id, "Instrument")
    status, idn = await anyio.to_thread.run_sync(
        probe, request.app.state.driver_factory, instrument.driver, instrument.resource
    )
    instrument.status = status
    if status is InstrumentStatus.ONLINE:
        instrument.idn = idn
        instrument.last_seen_at = datetime.now(UTC)
    await session.commit()
    return InstrumentOut.model_validate(instrument)
