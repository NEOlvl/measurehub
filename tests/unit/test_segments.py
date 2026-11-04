import numpy as np
import pytest
from pydantic import ValidationError

from measurehub.drivers.models import VnaSweepParams
from measurehub.drivers.segments import frequency_grid, split_sweep


@pytest.mark.parametrize(
    ("points", "size", "expected_segments", "last_count"),
    [(10_001, 50, 201, 1), (901, 50, 19, 1), (100, 50, 2, 50), (11, 10, 2, 1)],
)
def test_split_covers_every_point_once(
    points: int, size: int, expected_segments: int, last_count: int
) -> None:
    params = VnaSweepParams(points=points, segment_size=size)
    segments = split_sweep(params)
    assert len(segments) == expected_segments
    assert segments[-1].count == last_count
    assert [s.index for s in segments] == list(range(len(segments)))
    covered = np.concatenate([np.arange(s.first_idx, s.first_idx + s.count) for s in segments])
    assert covered.tolist() == list(range(points))


def test_segment_edges_match_the_global_grid() -> None:
    params = VnaSweepParams(start_hz=1e9, stop_hz=2e9, points=101, segment_size=30)
    grid = frequency_grid(params)
    for seg in split_sweep(params):
        assert seg.start_hz == grid[seg.first_idx]
        assert seg.stop_hz == grid[seg.first_idx + seg.count - 1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_hz": 5e9, "stop_hz": 5e9},
        {"stop_hz": 30e9},
        {"points": 10_002},
        {"points": 1},
        {"ifbw_hz": 5},
        {"segment_size": 5},
        {"averaging": 65},
        {"max_retries": 11},
    ],
)
def test_params_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        VnaSweepParams(**kwargs)
