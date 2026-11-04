import pytest

from measurehub.core.sample import SampleSpec, parse_sample


def test_parse_full_spec() -> None:
    spec = parse_sample("d_mm=3;eps=10,0.5; mu=1.5,0.2")
    assert spec == SampleSpec(0.003, (10.0, 0.5), (1.5, 0.2))


def test_parse_thickness_only() -> None:
    assert parse_sample("d_mm=2") == SampleSpec(0.002)


@pytest.mark.parametrize("text", ["foo=1", "d_mm", "eps=1", "d_mm=-1", "d_mm=abc", "mu=1,2,3"])
def test_parse_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        parse_sample(text)
