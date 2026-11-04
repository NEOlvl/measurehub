"""Splitting a frequency sweep into segments."""

from __future__ import annotations

import numpy as np

from measurehub.drivers.models import FloatArray, Segment, VnaSweepParams


def frequency_grid(params: VnaSweepParams) -> FloatArray:
    """Frequencies of all sweep points."""
    return np.linspace(params.start_hz, params.stop_hz, params.points)


def split_sweep(params: VnaSweepParams) -> list[Segment]:
    """Cut the sweep into segments of ``segment_size`` points (the last one may be shorter)."""
    grid = frequency_grid(params)
    segments: list[Segment] = []
    for index, first in enumerate(range(0, params.points, params.segment_size)):
        count = min(params.segment_size, params.points - first)
        segments.append(
            Segment(
                index=index,
                first_idx=first,
                count=count,
                start_hz=float(grid[first]),
                stop_hz=float(grid[first + count - 1]),
            )
        )
    return segments
