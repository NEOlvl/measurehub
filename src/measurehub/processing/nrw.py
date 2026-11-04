"""Nicolson-Ross-Weir extraction of complex permittivity and permeability (TEM line).

Convention: ``eps = eps' - j*eps''`` and ``mu = mu' - j*mu''``; the processor returns the
non-negative loss factors ``eps''`` and ``mu''``.

Algorithm, per frequency point:

1. ``X = (S11^2 - S21^2 + 1) / (2 S11)``; ``G = X +- sqrt(X^2 - 1)`` with ``|G| <= 1``.
2. ``T = (S11 + S21 - G) / (1 - (S11 + S21) G)`` (transmission of the slab).
3. ``z = (1 + G) / (1 - G) = sqrt(mu / eps)``; ``n = sqrt(mu * eps) = j ln(T) / (k0 d)``.
4. ``mu = n z``, ``eps = n / z``.

The phase of ``T`` is unwrapped along frequency, so the branch of the logarithm is resolved
as long as the first point lies in the first branch. Points where the unwrapped phase delay
reaches pi (thickness >= half a wavelength in the material) are flagged ``phase_ambiguity``:
the one-branch assumption of v1 no longer holds there.
"""

from __future__ import annotations

import numpy as np

from measurehub.processing.base import (
    BoolArray,
    ComplexArray,
    FloatArray,
    ProcessorInput,
    ProcessorResult,
)

C0 = 299_792_458.0


def reflection_coefficient(s11: ComplexArray, s21: ComplexArray) -> ComplexArray:
    """Reflection coefficient G of the half-space, choosing the root with ``|G| <= 1``."""
    x = (s11**2 - s21**2 + 1) / (2 * s11)
    root = np.sqrt(x * x - 1)
    gamma = x + root
    return np.where(np.abs(gamma) > 1, x - root, gamma).astype(np.complex128)


def extract(
    freq_hz: FloatArray, s11: ComplexArray, s21: ComplexArray, thickness_m: float
) -> tuple[ComplexArray, ComplexArray, BoolArray]:
    """Return complex ``eps``, ``mu`` and the ``phase_ambiguity`` flag per point."""
    with np.errstate(all="ignore"):
        gamma = reflection_coefficient(s11, s21)
        t = (s11 + s21 - gamma) / (1 - (s11 + s21) * gamma)
        z = (1 + gamma) / (1 - gamma)

        phase = np.full(freq_hz.shape, np.nan)
        finite = np.isfinite(t) & (t != 0)
        phase[finite] = np.unwrap(np.angle(t[finite]))

        k0d = 2 * np.pi * freq_hz * thickness_m / C0
        n = (1j * np.log(np.abs(t)) - phase) / k0d
        mu = (n * z).astype(np.complex128)
        eps = (n / z).astype(np.complex128)
        ambiguous = np.asarray(-phase >= np.pi, dtype=np.bool_)
    return eps, mu, ambiguous


def _mean(values: FloatArray, mask: BoolArray) -> float:
    return float(np.mean(values[mask])) if mask.any() else float("nan")


class NrwProcessor:
    name = "nrw"
    version = "1.0"

    def process(self, data: ProcessorInput) -> ProcessorResult:
        if data.thickness_m is None or data.thickness_m <= 0:
            raise ValueError("NRW requires a positive sample thickness")
        eps, mu, ambiguous = extract(data.freq_hz, data.s11, data.s21, data.thickness_m)
        invalid = ~(np.isfinite(eps) & np.isfinite(mu))
        columns = {
            "eps_re": np.asarray(eps.real, dtype=np.float64),
            "eps_im": np.asarray(-eps.imag, dtype=np.float64),
            "mu_re": np.asarray(mu.real, dtype=np.float64),
            "mu_im": np.asarray(-mu.imag, dtype=np.float64),
        }
        good = ~(invalid | ambiguous)
        summary = {f"{name}_mean": _mean(values, good) for name, values in columns.items()}
        summary["flagged_fraction"] = float(np.mean(~good))
        return ProcessorResult(
            self.name,
            self.version,
            data.freq_hz,
            columns,
            flags={"phase_ambiguity": ambiguous, "invalid": invalid},
            summary=summary,
        )
