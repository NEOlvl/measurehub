"""SCPI command interpreter of the simulated VNA (independent of the transport)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from simulator.model import (
    DEMO_SAMPLE,
    ComplexArray,
    FloatArray,
    Sample,
    add_noise,
    noise_sigma,
    slab_sparameters,
)

IDN = "MeasureHub,SIM-VNA-01,000001,1.0"
FREQ_MIN_HZ = 1e6
FREQ_MAX_HZ = 20e9
POINTS_MAX = 100_001
IFBW_MIN_HZ = 1.0
IFBW_MAX_HZ = 1e6
AVERAGING_MAX = 1000

DEFAULT_START_HZ = 1e9
DEFAULT_STOP_HZ = 10e9
DEFAULT_POINTS = 901
DEFAULT_IFBW_HZ = 1000.0
DEFAULT_AVERAGING = 1


class ScpiError(Exception):
    """Instrument error that is pushed to the error queue (``SYST:ERR?``)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Reply:
    """Response line (``None`` text means no output) sent after ``delay_s`` seconds."""

    text: str | None = None
    delay_s: float = 0.0


@dataclass(frozen=True, slots=True)
class _Entry:
    nodes: tuple[tuple[str, str], ...]  # (short form, long form) per header node
    get: Callable[[], str | Reply] | None
    set: Callable[[str], None] | None
    event: Callable[[], None] | None

    def matches(self, header: list[str]) -> bool:
        return len(header) == len(self.nodes) and all(
            node in forms for node, forms in zip(header, self.nodes, strict=True)
        )


def _compile(pattern: str) -> tuple[tuple[str, str], ...]:
    """``SENSe:FREQuency:STARt`` -> (("SENS", "SENSE"), ("FREQ", "FREQUENCY"), ...)."""
    nodes = []
    for node in pattern.split(":"):
        short = "".join(ch for ch in node if not ch.islower()).upper()
        nodes.append((short, node.upper()))
    return tuple(nodes)


def _parse_float(arg: str) -> float:
    try:
        return float(arg)
    except ValueError:
        raise ScpiError(-104, "Data type error") from None


def _parse_int(arg: str) -> int:
    value = _parse_float(arg)
    if not value.is_integer():
        raise ScpiError(-104, "Data type error")
    return int(value)


def _check_range(value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ScpiError(-222, "Data out of range")


def _fmt(value: float) -> str:
    return format(value, ".12g")


def _join_floats(values: FloatArray, spec: str) -> str:
    return ",".join(format(v, spec) for v in values.tolist())


class ScpiEngine:
    """Simulated VNA state machine: ``process_line`` maps one input line to replies."""

    def __init__(
        self,
        sample: Sample = DEMO_SAMPLE,
        *,
        seed: int | None = None,
        noise_k: float = 1e-3,
        max_sweep_s: float = 5.0,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sample = sample
        self.noise_k = noise_k
        self.max_sweep_s = max_sweep_s
        self.time_scale = time_scale
        self._clock = clock
        self._rng = np.random.default_rng(seed)
        self._errors: deque[ScpiError] = deque(maxlen=20)
        self._data: tuple[FloatArray, ComplexArray, ComplexArray] | None = None
        self._busy_until = 0.0
        self._reset()
        self._table = self._build_table()

    # -- state ---------------------------------------------------------------------------------

    def _reset(self) -> None:
        self.start_hz = DEFAULT_START_HZ
        self.stop_hz = DEFAULT_STOP_HZ
        self.points = DEFAULT_POINTS
        self.ifbw_hz = DEFAULT_IFBW_HZ
        self.averaging = DEFAULT_AVERAGING
        self._data = None
        self._busy_until = 0.0

    @property
    def sweep_time_s(self) -> float:
        """Simulated sweep duration: ``points * averaging / ifbw``, capped, then time-scaled."""
        raw = self.points * self.averaging / self.ifbw_hz
        return min(raw, self.max_sweep_s) * self.time_scale

    def _remaining_s(self) -> float:
        return max(0.0, self._busy_until - self._clock())

    def _grid(self) -> FloatArray:
        return np.linspace(self.start_hz, self.stop_hz, self.points)

    # -- command table -------------------------------------------------------------------------

    def _build_table(self) -> list[_Entry]:
        table: list[_Entry] = []

        def add(
            pattern: str,
            *,
            get: Callable[[], str | Reply] | None = None,
            set: Callable[[str], None] | None = None,
            event: Callable[[], None] | None = None,
        ) -> None:
            table.append(_Entry(_compile(pattern), get, set, event))

        add("*IDN", get=lambda: IDN)
        add("*RST", event=self._reset)
        add("*CLS", event=self._errors.clear)
        add("*OPC", get=lambda: Reply("1", self._remaining_s()))
        add("SYSTem:ERRor", get=self._pop_error)
        add(
            "SENSe:FREQuency:STARt",
            get=lambda: _fmt(self.start_hz),
            set=lambda a: self._set_freq("start_hz", a),
        )
        add(
            "SENSe:FREQuency:STOP",
            get=lambda: _fmt(self.stop_hz),
            set=lambda a: self._set_freq("stop_hz", a),
        )
        add("SENSe:SWEep:POINts", get=lambda: str(self.points), set=self._set_points)
        add("SENSe:BWIDth", get=lambda: _fmt(self.ifbw_hz), set=self._set_ifbw)
        add("SENSe:AVERage:COUNt", get=lambda: str(self.averaging), set=self._set_averaging)
        add("INITiate:IMMediate", event=self._init_imm)
        add("SENSe:FREQuency:DATA", get=lambda: _join_floats(self._grid(), ".15g"))
        add("CALCulate:DATA:SDATa", get=self._sdata)
        add("SIM:SAMPle:EPS", get=lambda: self._get_pair(self.sample.eps), set=self._set_eps)
        add("SIM:SAMPle:MU", get=lambda: self._get_pair(self.sample.mu), set=self._set_mu)
        add(
            "SIM:SAMPle:THICkness",
            get=lambda: _fmt(self.sample.thickness_m),
            set=self._set_thickness,
        )
        return table

    def _pop_error(self) -> str:
        if not self._errors:
            return '0,"No error"'
        error = self._errors.popleft()
        return f'{error.code},"{error.message}"'

    def _set_freq(self, attr: str, arg: str) -> None:
        value = _parse_float(arg)
        _check_range(value, FREQ_MIN_HZ, FREQ_MAX_HZ)
        setattr(self, attr, value)

    def _set_points(self, arg: str) -> None:
        value = _parse_int(arg)
        _check_range(value, 1, POINTS_MAX)
        self.points = value

    def _set_ifbw(self, arg: str) -> None:
        value = _parse_float(arg)
        _check_range(value, IFBW_MIN_HZ, IFBW_MAX_HZ)
        self.ifbw_hz = value

    def _set_averaging(self, arg: str) -> None:
        value = _parse_int(arg)
        _check_range(value, 1, AVERAGING_MAX)
        self.averaging = value

    @staticmethod
    def _get_pair(value: complex) -> str:
        return f"{_fmt(value.real)},{_fmt(-value.imag)}"

    @staticmethod
    def _parse_pair(arg: str) -> tuple[float, float]:
        parts = arg.split(",")
        if len(parts) != 2:
            raise ScpiError(-108, "Parameter not allowed")
        real, loss = _parse_float(parts[0]), _parse_float(parts[1])
        if real <= 0 or loss < 0:
            raise ScpiError(-222, "Data out of range")
        return real, loss

    def _set_eps(self, arg: str) -> None:
        real, loss = self._parse_pair(arg)
        self.sample = Sample(complex(real, -loss), self.sample.mu, self.sample.thickness_m)

    def _set_mu(self, arg: str) -> None:
        real, loss = self._parse_pair(arg)
        self.sample = Sample(self.sample.eps, complex(real, -loss), self.sample.thickness_m)

    def _set_thickness(self, arg: str) -> None:
        value = _parse_float(arg)
        if value < 0:
            raise ScpiError(-222, "Data out of range")
        self.sample = Sample(self.sample.eps, self.sample.mu, value)

    def _init_imm(self) -> None:
        if self.start_hz > self.stop_hz:
            raise ScpiError(-221, "Settings conflict")
        freq = self._grid()
        s11, s21 = slab_sparameters(freq, self.sample)
        sigma = noise_sigma(self.ifbw_hz, self.averaging, self.noise_k)
        self._data = (freq, add_noise(s11, sigma, self._rng), add_noise(s21, sigma, self._rng))
        self._busy_until = self._clock() + self.sweep_time_s

    def _sdata(self) -> Reply:
        if self._data is None:
            raise ScpiError(-230, "Data corrupt or stale")
        _, s11, s21 = self._data
        flat = np.concatenate([np.column_stack([s.real, s.imag]).ravel() for s in (s11, s21)])
        return Reply(_join_floats(flat, ".9e"), self._remaining_s())

    # -- execution -----------------------------------------------------------------------------

    def process_line(self, line: str) -> list[Reply]:
        """Execute ``;``-separated commands; return the replies in order.

        A failed query still answers with an empty line so that the client stays in lockstep and
        finds the cause in ``SYST:ERR?`` instead of timing out.
        """
        replies: list[Reply] = []
        for command in line.split(";"):
            command = command.strip()
            if command:
                reply = self._execute(command)
                if reply is not None:
                    replies.append(reply)
        return replies

    def _execute(self, command: str) -> Reply | None:
        parts = command.split(None, 1)
        header = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else None
        query = header.endswith("?")
        if query:
            header = header[:-1]
        try:
            return self._dispatch(header, arg, query)
        except ScpiError as error:
            self._errors.append(error)
            return Reply("") if query else None

    def _dispatch(self, header: str, arg: str | None, query: bool) -> Reply | None:
        nodes = header.lstrip(":").upper().split(":")
        for entry in self._table:
            if not entry.matches(nodes):
                continue
            if query:
                if entry.get is None:
                    break
                result = entry.get()
                return result if isinstance(result, Reply) else Reply(result)
            if entry.event is not None:
                entry.event()
                return None
            if entry.set is None:
                break
            if arg is None:
                raise ScpiError(-109, "Missing parameter")
            entry.set(arg)
            return None
        raise ScpiError(-113, "Undefined header")
