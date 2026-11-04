"""Transport layer: a minimal text-command interface and its PyVISA implementation."""

from __future__ import annotations

import contextlib
import time
from typing import Any, Protocol

import pyvisa
from pyvisa import constants
from pyvisa.errors import VisaIOError

from measurehub.drivers.errors import InstrumentConnectionError, InstrumentTimeoutError


class Transport(Protocol):
    """Line-oriented command channel to an instrument."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, command: str) -> None: ...

    def query(self, command: str, timeout_ms: int | None = None) -> str: ...


class VisaTransport:
    """PyVISA transport (default backend: pyvisa-py) with domain error translation."""

    def __init__(
        self,
        resource: str,
        *,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
        backend: str = "@py",
        connect_attempts: int = 3,
        retry_delay_s: float = 0.5,
    ) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self.read_termination = read_termination
        self.write_termination = write_termination
        self.backend = backend
        self.connect_attempts = connect_attempts
        self.retry_delay_s = retry_delay_s
        self._rm: Any = None
        self._inst: Any = None

    def open(self) -> None:
        """Open the resource, retrying a few times before giving up."""
        self.close()
        last: Exception | None = None
        for attempt in range(self.connect_attempts):
            try:
                self._rm = pyvisa.ResourceManager(self.backend)
                inst = self._rm.open_resource(self.resource, open_timeout=self.timeout_ms)
                inst.timeout = self.timeout_ms
                inst.read_termination = self.read_termination
                inst.write_termination = self.write_termination
                inst.chunk_size = 1 << 20
                self._inst = inst
                return
            except Exception as exc:
                last = exc
                self.close()
                if attempt + 1 < self.connect_attempts:
                    time.sleep(self.retry_delay_s)
        raise InstrumentConnectionError(f"cannot open {self.resource}: {last}") from last

    def reconnect(self) -> None:
        self.close()
        self.open()

    def close(self) -> None:
        for obj in (self._inst, self._rm):
            if obj is not None:
                with contextlib.suppress(VisaIOError, OSError, pyvisa.Error):
                    obj.close()
        self._inst = None
        self._rm = None

    def write(self, command: str) -> None:
        inst = self._require()
        try:
            inst.write(command)
        except (VisaIOError, OSError, pyvisa.Error) as exc:
            raise self._translate(exc) from exc

    def query(self, command: str, timeout_ms: int | None = None) -> str:
        inst = self._require()
        try:
            if timeout_ms is not None:
                inst.timeout = timeout_ms
            return str(inst.query(command)).strip()
        except (VisaIOError, OSError, pyvisa.Error) as exc:
            raise self._translate(exc) from exc
        finally:
            if timeout_ms is not None:
                with contextlib.suppress(VisaIOError, OSError, pyvisa.Error):
                    inst.timeout = self.timeout_ms

    def _require(self) -> Any:
        if self._inst is None:
            raise InstrumentConnectionError("transport is not open")
        return self._inst

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        if isinstance(exc, VisaIOError) and exc.error_code == constants.StatusCode.error_timeout:
            return InstrumentTimeoutError(str(exc))
        return InstrumentConnectionError(str(exc))
