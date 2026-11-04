"""Processor protocol and the data types it exchanges (pure, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]
BoolArray = NDArray[np.bool_]


@dataclass(frozen=True, slots=True)
class ProcessorInput:
    """Raw S-parameters of a sweep; ``thickness_m`` is the sample thickness if known."""

    freq_hz: FloatArray
    s11: ComplexArray
    s21: ComplexArray
    thickness_m: float | None = None


@dataclass(frozen=True, slots=True)
class ProcessorResult:
    """Output columns per frequency point, per-point quality flags and a scalar summary."""

    processor: str
    version: str
    freq_hz: FloatArray
    columns: dict[str, FloatArray]
    flags: dict[str, BoolArray] = field(default_factory=dict)
    summary: dict[str, float] = field(default_factory=dict)


class Processor(Protocol):
    name: str
    version: str

    def process(self, data: ProcessorInput) -> ProcessorResult: ...
