"""Accumulates the points of a sweep for the live graphs (no Qt, easy to test)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


def to_db(magnitude: FloatArray) -> FloatArray:
    return np.asarray(20 * np.log10(np.maximum(magnitude, 1e-12)), dtype=np.float64)


class SweepBuffer:
    """|S11| and |S21| in dB over a frequency axis that is known in advance.

    Points are stored by their index, so the same point arriving twice (a segment event and the
    points fetched after a reconnect) just overwrites itself, and gaps stay ``nan``.
    """

    def __init__(self, freq_hz: Sequence[float] | FloatArray) -> None:
        self.freq_hz: FloatArray = np.asarray(freq_hz, dtype=np.float64)
        self.s11_db: FloatArray = np.full(self.freq_hz.shape, np.nan)
        self.s21_db: FloatArray = np.full(self.freq_hz.shape, np.nan)

    @classmethod
    def for_params(cls, params: dict[str, Any]) -> SweepBuffer:
        """Buffer for the sweep described by a job's ``params``."""
        return cls(np.linspace(params["start_hz"], params["stop_hz"], int(params["points"])))

    @property
    def filled(self) -> int:
        return int(np.count_nonzero(np.isfinite(self.s21_db)))

    def add_segment(self, from_idx: int, s11_db: Sequence[float], s21_db: Sequence[float]) -> None:
        end = from_idx + len(s11_db)
        self.s11_db[from_idx:end] = s11_db
        self.s21_db[from_idx:end] = s21_db

    def add_points(self, points: Iterable[dict[str, Any]]) -> None:
        """Points as returned by ``GET /jobs/{id}/points``."""
        rows = list(points)
        if not rows:
            return
        idx = np.array([p["idx"] for p in rows], dtype=int)
        s11 = np.hypot([p["s11_re"] for p in rows], [p["s11_im"] for p in rows])
        s21 = np.hypot([p["s21_re"] for p in rows], [p["s21_im"] for p in rows])
        self.s11_db[idx] = to_db(s11)
        self.s21_db[idx] = to_db(s21)
