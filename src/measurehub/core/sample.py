"""Sample description given on the command line: ``d_mm=3;eps=10,0.5;mu=1.5,0.2``."""

from __future__ import annotations

from dataclasses import dataclass

Pair = tuple[float, float]


@dataclass(frozen=True, slots=True)
class SampleSpec:
    """Thickness plus, when known (simulator), the true ``(eps', eps'')`` and ``(mu', mu'')``."""

    thickness_m: float | None = None
    eps: Pair | None = None
    mu: Pair | None = None


def _pair(text: str, key: str) -> Pair:
    parts = text.split(",")
    if len(parts) != 2:
        raise ValueError(f"{key} expects two comma-separated numbers, got {text!r}")
    return float(parts[0]), float(parts[1])


def parse_sample(text: str) -> SampleSpec:
    """Parse ``key=value`` items separated by ``;`` (keys: ``d_mm``, ``eps``, ``mu``)."""
    values: dict[str, str] = {}
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or key not in {"d_mm", "eps", "mu"}:
            raise ValueError(f"invalid sample item {item!r}; keys: d_mm, eps, mu")
        values[key] = value.strip()
    thickness = float(values["d_mm"]) / 1000 if "d_mm" in values else None
    if thickness is not None and thickness <= 0:
        raise ValueError("d_mm must be positive")
    return SampleSpec(
        thickness_m=thickness,
        eps=_pair(values["eps"], "eps") if "eps" in values else None,
        mu=_pair(values["mu"], "mu") if "mu" in values else None,
    )
