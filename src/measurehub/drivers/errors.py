"""Domain exceptions raised by drivers; the job engine decides whether to retry."""

from __future__ import annotations


class DriverError(Exception):
    """Base class of all driver-level failures."""


class InstrumentConnectionError(DriverError):
    """The link to the instrument could not be opened or was lost."""


class InstrumentTimeoutError(DriverError):
    """The instrument did not answer in time."""


class InstrumentError(DriverError):
    """The instrument reported an error (``SYST:ERR?``) or sent a malformed response."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
