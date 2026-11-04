"""Forward physical model: a slab of material in a coaxial (TEM) line.

Complex permittivity and permeability follow the engineering convention
``e^{+jwt}``: a lossy material is ``eps = eps' - j*eps''`` with ``eps'' >= 0``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

C0 = 299_792_458.0

FloatArray = NDArray[np.float64]
ComplexArray = NDArray[np.complex128]


@dataclass(frozen=True, slots=True)
class Sample:
    """Slab of thickness ``thickness_m`` with complex ``eps`` and ``mu`` (loss: negative imag)."""

    eps: complex = 1 + 0j
    mu: complex = 1 + 0j
    thickness_m: float = 0.0

    def __post_init__(self) -> None:
        if self.eps.real <= 0 or self.mu.real <= 0:
            raise ValueError("eps' and mu' must be positive")
        if self.eps.imag > 0 or self.mu.imag > 0:
            raise ValueError("passive material: eps'' and mu'' must be non-negative")
        if self.thickness_m < 0:
            raise ValueError("thickness must be non-negative")

    @classmethod
    def from_parts(
        cls, eps_re: float, eps_loss: float, mu_re: float, mu_loss: float, thickness_m: float
    ) -> Sample:
        """Build from (eps', eps'') and (mu', mu'') with non-negative loss factors."""
        return cls(complex(eps_re, -eps_loss), complex(mu_re, -mu_loss), thickness_m)


DEMO_SAMPLE = Sample.from_parts(10.0, 0.5, 1.5, 0.2, 0.003)


def slab_sparameters(freq_hz: FloatArray, sample: Sample) -> tuple[ComplexArray, ComplexArray]:
    """S11 and S21 of the slab at the given frequencies (no noise)."""
    f = np.asarray(freq_hz, dtype=np.float64)
    z = np.sqrt(sample.mu / sample.eps)
    gamma = (z - 1) / (z + 1)
    n = np.sqrt(sample.mu * sample.eps)
    t = np.exp(-1j * 2 * np.pi * f * sample.thickness_m * n / C0)
    denom = 1 - gamma**2 * t**2
    s11 = gamma * (1 - t**2) / denom
    s21 = t * (1 - gamma**2) / denom
    return s11.astype(np.complex128), s21.astype(np.complex128)


def noise_sigma(ifbw_hz: float, averaging: int, k: float) -> float:
    """Per-component noise sigma: ``k * sqrt(IFBW / 1000) / sqrt(averaging)``."""
    return k * float(np.sqrt(ifbw_hz / 1000.0)) / float(np.sqrt(averaging))


def add_noise(values: ComplexArray, sigma: float, rng: np.random.Generator) -> ComplexArray:
    """Add complex Gaussian noise with ``sigma`` on both real and imaginary parts."""
    if sigma == 0:
        return values
    noise = rng.standard_normal(values.shape) + 1j * rng.standard_normal(values.shape)
    return (values + sigma * noise).astype(np.complex128)
