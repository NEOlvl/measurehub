"""Driver tests on a fake transport (no sockets)."""

from __future__ import annotations

import numpy as np
import pytest
from pyvisa import constants
from pyvisa.errors import VisaIOError

from measurehub.drivers.errors import (
    InstrumentConnectionError,
    InstrumentError,
    InstrumentTimeoutError,
)
from measurehub.drivers.models import Segment, VnaSweepParams
from measurehub.drivers.sim_vna import SimVnaDriver
from measurehub.drivers.transport import VisaTransport


class FakeTransport:
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.written: list[str] = []
        self.timeouts: dict[str, int | None] = {}

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, command: str) -> None:
        self.written.append(command)

    def query(self, command: str, timeout_ms: int | None = None) -> str:
        self.written.append(command)
        self.timeouts[command] = timeout_ms
        return (
            self.answers[command].pop(0)
            if len(self.answers[command]) > 1
            else (self.answers[command][0])
        )


SEGMENT = Segment(index=1, first_idx=2, count=2, start_hz=3e9, stop_hz=4e9)
PARAMS = VnaSweepParams(start_hz=1e9, stop_hz=5e9, points=5)
NO_ERROR = '0,"No error"'


def make_driver(answers: dict[str, list[str]]) -> tuple[SimVnaDriver, FakeTransport]:
    transport = FakeTransport({"SYST:ERR?": [NO_ERROR], "*OPC?": ["1"]})
    driver = SimVnaDriver(transport)
    driver.configure(PARAMS)
    transport.answers.update({k: list(v) for k, v in answers.items()})
    return driver, transport


def test_identify_parses_idn() -> None:
    driver = SimVnaDriver(FakeTransport({"*IDN?": ["MeasureHub,SIM-VNA-01,000001,1.0"]}))
    info = driver.identify()
    assert (info.manufacturer, info.model, info.serial, info.firmware) == (
        "MeasureHub",
        "SIM-VNA-01",
        "000001",
        "1.0",
    )


def test_identify_rejects_garbage() -> None:
    driver = SimVnaDriver(FakeTransport({"*IDN?": ["nonsense"]}))
    with pytest.raises(InstrumentError):
        driver.identify()


def test_measure_segment_sends_scpi_and_parses_pairs() -> None:
    # S11 = (0.1+0.2j, 0.3+0.4j), S21 = (0.5+0.6j, 0.7+0.8j)
    reply = "0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8"
    driver, transport = make_driver({"CALC:DATA:SDAT?": [reply]})
    data = driver.measure_segment(SEGMENT)
    assert np.allclose(data.s11, [0.1 + 0.2j, 0.3 + 0.4j])
    assert np.allclose(data.s21, [0.5 + 0.6j, 0.7 + 0.8j])
    assert np.allclose(data.freq_hz, [3e9, 4e9])
    assert "SENS:FREQ:STAR 3000000000.0" in transport.written
    assert "SENS:SWE:POIN 2" in transport.written
    assert transport.written.index("INIT:IMM") < transport.written.index("*OPC?")
    assert (transport.timeouts["*OPC?"] or 0) > 5000


def test_wrong_value_count_is_an_instrument_error() -> None:
    driver, _ = make_driver({"CALC:DATA:SDAT?": ["0.1,0.2"]})
    with pytest.raises(InstrumentError, match="expected 8 values"):
        driver.measure_segment(SEGMENT)


def test_instrument_error_queue_takes_precedence_over_parse_error() -> None:
    driver, _ = make_driver(
        {"CALC:DATA:SDAT?": [""], "SYST:ERR?": ['-230,"Data corrupt or stale"', NO_ERROR]}
    )
    with pytest.raises(InstrumentError) as info:
        driver.measure_segment(SEGMENT)
    assert info.value.code == -230


def test_check_errors_drains_queue() -> None:
    driver, _ = make_driver({"SYST:ERR?": ['-113,"Undefined header"', '-222,"Out"', NO_ERROR]})
    assert [e.code for e in driver.check_errors()] == [-113, -222]


def test_measure_before_configure_fails() -> None:
    driver = SimVnaDriver(FakeTransport({}))
    with pytest.raises(InstrumentError, match="not configured"):
        driver.measure_segment(SEGMENT)


# -- VisaTransport error translation ---------------------------------------------------------


class _BrokenResource:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.timeout = 0

    def query(self, command: str) -> str:
        raise self.exc

    def write(self, command: str) -> None:
        raise self.exc

    def close(self) -> None:
        pass


def _transport_with(exc: Exception) -> VisaTransport:
    transport = VisaTransport("TCPIP0::x::1::SOCKET")
    transport._inst = _BrokenResource(exc)
    return transport


def test_visa_timeout_becomes_timeout_error() -> None:
    transport = _transport_with(VisaIOError(constants.StatusCode.error_timeout))
    with pytest.raises(InstrumentTimeoutError):
        transport.query("*IDN?")


def test_visa_io_failure_becomes_connection_error() -> None:
    transport = _transport_with(VisaIOError(constants.StatusCode.error_connection_lost))
    with pytest.raises(InstrumentConnectionError):
        transport.write("*RST")


def test_os_error_becomes_connection_error() -> None:
    with pytest.raises(InstrumentConnectionError):
        _transport_with(ConnectionResetError("reset")).query("*IDN?")


def test_unopened_transport_raises_connection_error() -> None:
    with pytest.raises(InstrumentConnectionError, match="not open"):
        VisaTransport("TCPIP0::x::1::SOCKET").write("*RST")


def _open_and_query(transport: VisaTransport) -> None:
    transport.open()
    transport.query("*IDN?")


def test_unreachable_instrument_is_a_connection_error() -> None:
    """Windows reports a refused connection on open, Linux on the first exchange."""
    transport = VisaTransport(
        "TCPIP0::127.0.0.1::1::SOCKET", timeout_ms=300, connect_attempts=2, retry_delay_s=0.01
    )
    with pytest.raises(InstrumentConnectionError):
        _open_and_query(transport)
