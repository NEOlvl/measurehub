"""Processor registry keyed by name."""

from __future__ import annotations

from measurehub.processing.base import Processor
from measurehub.processing.nrw import NrwProcessor
from measurehub.processing.raw import RawProcessor

_REGISTRY: dict[str, Processor] = {}


def register(processor: Processor) -> None:
    if processor.name in _REGISTRY:
        raise ValueError(f"processor {processor.name!r} is already registered")
    _REGISTRY[processor.name] = processor


def get_processor(name: str) -> Processor:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown processor {name!r}; available: {known}") from None


def available() -> list[str]:
    return sorted(_REGISTRY)


register(RawProcessor())
register(NrwProcessor())
