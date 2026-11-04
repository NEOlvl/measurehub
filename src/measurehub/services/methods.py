"""Measurement types and validation of method parameters."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from measurehub.api.errors import ApiError, unprocessable
from measurehub.drivers.models import VnaSweepParams
from measurehub.processing import registry

MEASUREMENT_TYPES: dict[str, type[BaseModel]] = {"vna_sweep": VnaSweepParams}
PROCESSORS_REQUIRING_SAMPLE = frozenset({"nrw"})


def params_model(measurement_type: str) -> type[BaseModel]:
    try:
        return MEASUREMENT_TYPES[measurement_type]
    except KeyError:
        raise unprocessable(
            f"unknown measurement type {measurement_type!r}; available: {sorted(MEASUREMENT_TYPES)}"
        ) from None


def validate_params(measurement_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate against the measurement type's model; returns the full parameter set."""
    try:
        return params_model(measurement_type)(**params).model_dump()
    except ValidationError as exc:
        raise _validation_error(exc) from None


def validate_processor(name: str) -> None:
    if name not in registry.available():
        raise unprocessable(f"unknown processor {name!r}; available: {registry.available()}")


def _validation_error(exc: ValidationError) -> ApiError:
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return unprocessable("Invalid measurement parameters", errors)
