"""``raw`` processor: magnitude (dB) and phase (degrees) of S11 and S21."""

from __future__ import annotations

import numpy as np

from measurehub.processing.base import ComplexArray, FloatArray, ProcessorInput, ProcessorResult


def _db(values: ComplexArray) -> FloatArray:
    with np.errstate(divide="ignore"):
        return np.asarray(20 * np.log10(np.abs(values)), dtype=np.float64)


def _deg(values: ComplexArray) -> FloatArray:
    return np.asarray(np.degrees(np.angle(values)), dtype=np.float64)


class RawProcessor:
    name = "raw"
    version = "1.0"

    def process(self, data: ProcessorInput) -> ProcessorResult:
        columns = {
            "s11_db": _db(data.s11),
            "s11_deg": _deg(data.s11),
            "s21_db": _db(data.s21),
            "s21_deg": _deg(data.s21),
        }
        return ProcessorResult(self.name, self.version, data.freq_hz, columns)
