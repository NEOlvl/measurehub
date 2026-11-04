"""The interface every instrument driver implements."""

from __future__ import annotations

from typing import Protocol

from measurehub.drivers.errors import InstrumentError
from measurehub.drivers.models import InstrumentInfo, Segment, SegmentData, VnaSweepParams


class InstrumentDriver(Protocol):
    def connect(self) -> None: ...

    def close(self) -> None: ...

    def identify(self) -> InstrumentInfo: ...

    def reset(self) -> None: ...

    def configure(self, params: VnaSweepParams) -> None: ...

    def measure_segment(self, segment: Segment) -> SegmentData: ...

    def check_errors(self) -> list[InstrumentError]: ...
