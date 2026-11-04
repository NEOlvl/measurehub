import numpy as np
import pytest

from measurehub.processing import registry
from measurehub.processing.base import ProcessorInput, ProcessorResult
from measurehub.processing.nrw import NrwProcessor, reflection_coefficient
from measurehub.processing.raw import RawProcessor
from simulator.model import Sample, add_noise, noise_sigma, slab_sparameters

FREQ = np.linspace(1e9, 10e9, 901)

SAMPLES = [
    Sample.from_parts(10, 0.5, 1.5, 0.2, 0.003),
    Sample.from_parts(4, 0.1, 1.0, 0.0, 0.004),
    Sample.from_parts(15, 1.5, 2.5, 0.8, 0.002),
]


def process(sample: Sample, noise_k: float = 0.0) -> ProcessorResult:
    s11, s21 = slab_sparameters(FREQ, sample)
    if noise_k:
        rng = np.random.default_rng(0)
        sigma = noise_sigma(1000, 1, noise_k)
        s11, s21 = add_noise(s11, sigma, rng), add_noise(s21, sigma, rng)
    return NrwProcessor().process(ProcessorInput(FREQ, s11, s21, sample.thickness_m))


@pytest.mark.parametrize("sample", SAMPLES)
def test_noise_free_roundtrip_is_exact(sample: Sample) -> None:
    result = process(sample)
    assert np.allclose(result.columns["eps_re"], sample.eps.real, rtol=1e-6)
    assert np.allclose(result.columns["eps_im"], -sample.eps.imag, atol=1e-6)
    assert np.allclose(result.columns["mu_re"], sample.mu.real, rtol=1e-6)
    assert np.allclose(result.columns["mu_im"], -sample.mu.imag, atol=1e-6)
    assert result.summary["flagged_fraction"] == 0.0


@pytest.mark.parametrize("sample", SAMPLES)
def test_noisy_means_within_two_percent(sample: Sample) -> None:
    summary = process(sample, noise_k=1e-3).summary
    assert summary["eps_re_mean"] == pytest.approx(sample.eps.real, rel=0.02)
    assert summary["eps_im_mean"] == pytest.approx(-sample.eps.imag, rel=0.02)
    assert summary["mu_re_mean"] == pytest.approx(sample.mu.real, rel=0.02)
    if sample.mu.imag:
        assert summary["mu_im_mean"] == pytest.approx(-sample.mu.imag, rel=0.02)


def test_reflection_root_has_magnitude_at_most_one() -> None:
    s11, s21 = slab_sparameters(FREQ, SAMPLES[0])
    assert np.all(np.abs(reflection_coefficient(s11, s21)) <= 1 + 1e-12)


def test_thick_sample_is_flagged_but_branch_is_resolved_by_unwrapping() -> None:
    # n = sqrt(40) ~ 6.3: half-wave thickness is reached near 5 GHz for d = 4 mm
    sample = Sample.from_parts(40, 1.0, 1.0, 0.0, 0.004)
    result = process(sample)
    flagged = result.flags["phase_ambiguity"]
    assert 0 < flagged.sum() < flagged.size
    assert not flagged[0]
    assert result.flags["phase_ambiguity"][-1]
    assert result.summary["flagged_fraction"] > 0.3
    assert np.allclose(result.columns["eps_re"][~flagged], 40, rtol=1e-3)


def test_nrw_requires_thickness() -> None:
    s11, s21 = slab_sparameters(FREQ, SAMPLES[0])
    with pytest.raises(ValueError, match="thickness"):
        NrwProcessor().process(ProcessorInput(FREQ, s11, s21))


def test_degenerate_input_does_not_raise() -> None:
    # S11 == 0 (no reflection) makes the NRW inversion singular
    zeros = np.zeros_like(FREQ, dtype=np.complex128)
    result = NrwProcessor().process(ProcessorInput(FREQ, zeros, zeros + 1, 0.003))
    assert result.flags["invalid"].all()
    assert np.isnan(result.summary["eps_re_mean"])
    assert result.summary["flagged_fraction"] == 1.0


def test_raw_processor_columns() -> None:
    s11, s21 = slab_sparameters(FREQ, SAMPLES[0])
    result = RawProcessor().process(ProcessorInput(FREQ, s11, s21))
    assert set(result.columns) == {"s11_db", "s11_deg", "s21_db", "s21_deg"}
    assert np.allclose(result.columns["s21_db"], 20 * np.log10(np.abs(s21)))


def test_registry() -> None:
    assert registry.available() == ["nrw", "raw"]
    assert registry.get_processor("nrw").name == "nrw"
    with pytest.raises(KeyError, match="unknown processor"):
        registry.get_processor("nope")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(RawProcessor())
