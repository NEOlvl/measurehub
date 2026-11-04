"""Instrument connectivity check (blocking; run it in a worker thread)."""

from __future__ import annotations

from measurehub.db.models import InstrumentStatus
from measurehub.drivers.errors import DriverError, InstrumentError
from measurehub.drivers.registry import DriverFactory


def probe(
    factory: DriverFactory, driver_key: str, resource: str
) -> tuple[InstrumentStatus, str | None]:
    """Connect, ask ``*IDN?`` and disconnect; returns the resulting status and IDN string."""
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
