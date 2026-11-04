"""Whole-sweep measurement as a sequence of segments."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from measurehub.drivers.base import InstrumentDriver
from measurehub.drivers.models import SegmentData, SweepData, VnaSweepParams
from measurehub.drivers.segments import frequency_grid, split_sweep


def run_sweep(
    driver: InstrumentDriver,
    params: VnaSweepParams,
    on_segment: Callable[[SegmentData, int], None] | None = None,
) -> SweepData:
    """Reset and configure ``driver`` (which must be connected), then measure every segment.

    ``on_segment`` receives each finished segment and the total number of segments.
    """
    driver.reset()
    driver.configure(params)
    segments = split_sweep(params)
    s11 = np.empty(params.points, dtype=np.complex128)
    s21 = np.empty(params.points, dtype=np.complex128)
    for segment in segments:
        data = driver.measure_segment(segment)
        end = segment.first_idx + segment.count
        s11[segment.first_idx : end] = data.s11
        s21[segment.first_idx : end] = data.s21
        if on_segment is not None:
            on_segment(data, len(segments))
    return SweepData(frequency_grid(params), s11, s21)
