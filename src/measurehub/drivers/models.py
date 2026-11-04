"""Data models shared by drivers: sweep parameters, segments and measured data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]


class VnaSweepParams(BaseModel):
    """Parameters of the ``vna_sweep`` measurement type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_hz: float = Field(default=1e9, ge=1e6, le=20e9)
    stop_hz: float = Field(default=10e9, le=20e9)
    points: int = Field(default=901, ge=2, le=10_001)
    ifbw_hz: float = Field(default=1000.0, ge=10, le=100_000)
    segment_size: int = Field(default=50, ge=10, le=1000)
    averaging: int = Field(default=1, ge=1, le=64)
    max_retries: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def _stop_above_start(self) -> Self:
        if self.stop_hz <= self.start_hz:
            raise ValueError("stop_hz must be greater than start_hz")
        return self


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    """Parsed ``*IDN?`` answer."""

    manufacturer: str
    model: str
    serial: str
    firmware: str
    raw: str


@dataclass(frozen=True, slots=True)
class Segment:
    """Contiguous part ``[first_idx, first_idx + count)`` of a sweep, measured atomically."""

    index: int
    first_idx: int
    count: int
    start_hz: float
    stop_hz: float


@dataclass(frozen=True, slots=True)
class SegmentData:
    segment: Segment
    freq_hz: FloatArray
    s11: ComplexArray
    s21: ComplexArray


@dataclass(frozen=True, slots=True)
class SweepData:
    freq_hz: FloatArray
    s11: ComplexArray
    s21: ComplexArray
