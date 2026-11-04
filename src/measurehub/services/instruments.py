"""Instrument connectivity checks (blocking; the API runs them in a worker thread)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from measurehub.db.models import Instrument, InstrumentStatus
from measurehub.drivers.errors import DriverError, InstrumentError
from measurehub.drivers.registry import DriverFactory
from measurehub.worker.locks import lock_key


def probe(
    factory: DriverFactory, driver_key: str, resource: str
) -> tuple[InstrumentStatus, str | None]:
    """Connect, ask *IDN? and disconnect; returns the resulting status and IDN string."""
    driver = factory(driver_key, resource)
    try:
        driver.connect()
        return InstrumentStatus.ONLINE, driver.identify().raw
    except InstrumentError:
        return InstrumentStatus.ERROR, None
    except DriverError:
        return InstrumentStatus.OFFLINE, None
    finally:
        driver.close()


def poll_instruments(
    session_factory: sessionmaker[Session], redis: Any, factory: DriverFactory
) -> dict[uuid.UUID, InstrumentStatus]:
    """Check every enabled instrument that is not being measured; returns the new statuses.

    Instruments that are busy or hold the measurement lock are skipped (FR-03), and a status
    that became busy while the probe ran is not overwritten.
    """
    with session_factory() as session:
        instruments = session.execute(
            select(Instrument.id, Instrument.driver, Instrument.resource, Instrument.status).where(
                Instrument.is_enabled.is_(True)
            )
        ).all()
        result: dict[uuid.UUID, InstrumentStatus] = {}
        for instrument_id, driver, resource, status in instruments:
            if status is InstrumentStatus.BUSY or redis.exists(lock_key(instrument_id)):
                continue
            new_status, idn = probe(factory, driver, resource)
            values: dict[str, Any] = {"status": new_status}
            if new_status is InstrumentStatus.ONLINE:
                values |= {"idn": idn, "last_seen_at": datetime.now(UTC)}
            session.execute(
                update(Instrument)
                .where(Instrument.id == instrument_id, Instrument.status != InstrumentStatus.BUSY)
                .values(**values)
            )
            session.commit()
            result[instrument_id] = new_status
        return result
