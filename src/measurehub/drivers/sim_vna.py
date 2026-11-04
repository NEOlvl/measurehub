"""Driver for the SCPI VNA simulator (also a template for SCPI-compatible analyzers)."""

from __future__ import annotations

import numpy as np

from measurehub.drivers.errors import InstrumentError
from measurehub.drivers.models import (
    FloatArray,
    InstrumentInfo,
    Segment,
    SegmentData,
    VnaSweepParams,
)
from measurehub.drivers.segments import frequency_grid
from measurehub.drivers.transport import Transport

MAX_ERROR_QUEUE = 20
SWEEP_TIMEOUT_MARGIN_MS = 5000


class SimVnaDriver:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._params: VnaSweepParams | None = None

    def connect(self) -> None:
        self._transport.open()

    def close(self) -> None:
        self._transport.close()

    def identify(self) -> InstrumentInfo:
        raw = self._transport.query("*IDN?")
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 4:
            raise InstrumentError(0, f"unexpected *IDN? answer: {raw!r}")
        return InstrumentInfo(parts[0], parts[1], parts[2], parts[3], raw)

    def reset(self) -> None:
        self._transport.write("*RST")
        self._transport.write("*CLS")

    def configure(self, params: VnaSweepParams) -> None:
        self._transport.write(f"SENS:BWID {params.ifbw_hz!r}")
        self._transport.write(f"SENS:AVER:COUN {params.averaging}")
        self._raise_on_errors()
        self._params = params

    def measure_segment(self, segment: Segment) -> SegmentData:
        params = self._require_params()
        t = self._transport
        t.write(f"SENS:FREQ:STAR {segment.start_hz!r}")
        t.write(f"SENS:FREQ:STOP {segment.stop_hz!r}")
        t.write(f"SENS:SWE:POIN {segment.count}")
        t.write("INIT:IMM")
        sweep_ms = int(segment.count * params.averaging / params.ifbw_hz * 1000)
        timeout_ms = SWEEP_TIMEOUT_MARGIN_MS + 2 * sweep_ms
        t.query("*OPC?", timeout_ms=timeout_ms)
        raw = t.query("CALC:DATA:SDAT?", timeout_ms=timeout_ms)
        self._raise_on_errors()
        values = self._parse(raw, 4 * segment.count)
        s11 = values[: 2 * segment.count].reshape(-1, 2)
        s21 = values[2 * segment.count :].reshape(-1, 2)
        grid = frequency_grid(params)[segment.first_idx : segment.first_idx + segment.count]
        return SegmentData(
            segment=segment,
            freq_hz=grid,
            s11=(s11[:, 0] + 1j * s11[:, 1]).astype(np.complex128),
            s21=(s21[:, 0] + 1j * s21[:, 1]).astype(np.complex128),
        )

    def check_errors(self) -> list[InstrumentError]:
        """Drain the instrument error queue."""
        errors: list[InstrumentError] = []
        for _ in range(MAX_ERROR_QUEUE):
            answer = self._transport.query("SYST:ERR?")
            code_text, _, message = answer.partition(",")
            try:
                code = int(code_text)
            except ValueError:
                raise InstrumentError(0, f"unexpected SYST:ERR? answer: {answer!r}") from None
            if code == 0:
                break
            errors.append(InstrumentError(code, message.strip('"')))
        return errors

    def _raise_on_errors(self) -> None:
        errors = self.check_errors()
        if errors:
            raise errors[0]

    def _require_params(self) -> VnaSweepParams:
        if self._params is None:
            raise InstrumentError(0, "driver is not configured")
        return self._params

    @staticmethod
    def _parse(raw: str, expected: int) -> FloatArray:
        try:
            values = np.array(raw.split(","), dtype=np.float64) if raw else np.empty(0)
        except ValueError:
            raise InstrumentError(0, "malformed data response") from None
        if values.size != expected:
            raise InstrumentError(0, f"expected {expected} values, got {values.size}")
        return values
