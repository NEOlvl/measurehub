"""Driver registry: maps the ``driver`` key of an instrument to a driver instance."""

from __future__ import annotations

from collections.abc import Callable

from measurehub.drivers.base import InstrumentDriver
from measurehub.drivers.sim_vna import SimVnaDriver
from measurehub.drivers.transport import VisaTransport

DriverFactory = Callable[[str, str], InstrumentDriver]
"""``(driver_key, visa_resource) -> driver``; not connected yet."""


class UnknownDriverError(KeyError):
    """No driver is registered under the given key."""


def _sim_vna(resource: str, timeout_ms: int, connect_attempts: int) -> InstrumentDriver:
    transport = VisaTransport(resource, timeout_ms=timeout_ms, connect_attempts=connect_attempts)
    return SimVnaDriver(transport)


_DRIVERS: dict[str, Callable[[str, int, int], InstrumentDriver]] = {"sim_vna": _sim_vna}


def available() -> list[str]:
    return sorted(_DRIVERS)


def create_driver(
    key: str, resource: str, *, timeout_ms: int = 5000, connect_attempts: int = 3
) -> InstrumentDriver:
    try:
        factory = _DRIVERS[key]
    except KeyError:
        raise UnknownDriverError(f"unknown driver {key!r}; available: {available()}") from None
    return factory(resource, timeout_ms, connect_attempts)
