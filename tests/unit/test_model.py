import numpy as np
import pytest

from simulator.model import C0, Sample, add_noise, noise_sigma, slab_sparameters

FREQ = np.linspace(1e9, 10e9, 101)


def test_empty_sample_is_transparent() -> None:
    s11, s21 = slab_sparameters(FREQ, Sample(1 + 0j, 1 + 0j, 0.003))
    assert np.allclose(np.abs(s21), 1.0)
    assert np.allclose(s11, 0.0, atol=1e-12)


@pytest.mark.parametrize("sample", [Sample(4 + 0j, 1 + 0j, 0.004), Sample(10 + 0j, 2 + 0j, 0.002)])
def test_lossless_sample_conserves_energy(sample: Sample) -> None:
    s11, s21 = slab_sparameters(FREQ, sample)
    assert np.allclose(np.abs(s11) ** 2 + np.abs(s21) ** 2, 1.0)


def test_lossy_sample_absorbs() -> None:
    s11, s21 = slab_sparameters(FREQ, Sample.from_parts(10, 1, 1.5, 0.3, 0.003))
    assert np.all(np.abs(s11) ** 2 + np.abs(s21) ** 2 < 1.0)


def test_half_wave_slab_is_matched() -> None:
    # d = lambda_g / 2 for eps=4, mu=1: n=2, f = c / (2 d n)
    d = 0.01
    f = np.array([C0 / (2 * d * 2.0)])
    s11, s21 = slab_sparameters(f, Sample(4 + 0j, 1 + 0j, d))
    assert abs(s11[0]) < 1e-9
    assert abs(s21[0]) == pytest.approx(1.0)


def test_thick_lossy_slab_attenuates_by_expected_amount() -> None:
    # matched material (eps = mu) has no reflection and n = 3 - 0.3j: |S21| = exp(-k0 d 0.3)
    sample = Sample.from_parts(3, 0.3, 3, 0.3, 0.01)
    f = np.array([5e9])
    s11, s21 = slab_sparameters(f, sample)
    assert abs(s11[0]) < 1e-12
    assert abs(s21[0]) == pytest.approx(np.exp(-2 * np.pi * 5e9 * 0.01 * 0.3 / C0))


def test_sample_rejects_active_material() -> None:
    with pytest.raises(ValueError, match="passive"):
        Sample(2 + 0.1j, 1 + 0j, 0.001)


def test_noise_sigma_scales_with_bandwidth_and_averaging() -> None:
    base = noise_sigma(1000, 1, 1e-3)
    assert base == pytest.approx(1e-3)
    assert noise_sigma(4000, 1, 1e-3) == pytest.approx(2 * base)
    assert noise_sigma(1000, 4, 1e-3) == pytest.approx(base / 2)


def test_added_noise_has_requested_sigma() -> None:
    rng = np.random.default_rng(0)
    noisy = add_noise(np.zeros(200_000, dtype=np.complex128), 0.01, rng)
    assert np.std(noisy.real) == pytest.approx(0.01, rel=0.02)
    assert np.std(noisy.imag) == pytest.approx(0.01, rel=0.02)
