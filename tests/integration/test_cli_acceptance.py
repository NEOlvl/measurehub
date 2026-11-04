"""Phase 1 acceptance: the CLI sweeps 10 001 points via pyvisa-py and NRW recovers eps and mu."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from measurehub.cli import main

CASES = [
    ("d_mm=3;eps=10,0.5;mu=1.5,0.2", (10.0, 0.5, 1.5, 0.2)),
    ("d_mm=4;eps=4,0.1;mu=1,0.05", (4.0, 0.1, 1.0, 0.05)),
    ("d_mm=2;eps=15,1.5;mu=2.5,0.8", (15.0, 1.5, 2.5, 0.8)),
]


@pytest.mark.parametrize(("sample", "truth"), CASES)
def test_cli_measure_recovers_eps_and_mu(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sample: str, truth: tuple[float, ...]
) -> None:
    out = tmp_path / "result.csv"
    code = main(
        ["measure", "--sample", sample, "--points", "10001", "--seed", "3", "--out", str(out)]
    )
    assert code == 0
    assert "10001 points" in capsys.readouterr().out

    table = np.genfromtxt(out, delimiter=",", names=True)
    assert table.shape == (10_001,)
    assert table["freq_hz"][0] == pytest.approx(1e9)
    assert table["freq_hz"][-1] == pytest.approx(10e9)
    assert not table["phase_ambiguity"].any()
    assert not table["invalid"].any()

    for column, expected in zip(("eps_re", "eps_im", "mu_re", "mu_im"), truth, strict=True):
        assert np.mean(table[column]) == pytest.approx(expected, rel=0.02), column


def test_cli_raw_processor_needs_no_sample(tmp_path: Path) -> None:
    out = tmp_path / "raw.csv"
    assert main(["measure", "--processor", "raw", "--points", "101", "--out", str(out)]) == 0
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert header == "freq_hz,s11_db,s11_deg,s21_db,s21_deg"


def test_cli_reports_unreachable_instrument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "measure",
            "--resource",
            "TCPIP0::127.0.0.1::1::SOCKET",
            "--sample",
            "d_mm=3",
            "--timeout-ms",
            "300",
            "--out",
            str(tmp_path / "x.csv"),
        ]
    )
    assert code == 1
    # "cannot open" on Windows, the refused connection on the first exchange on Linux
    assert capsys.readouterr().err.startswith("error: ")


def test_cli_nrw_without_thickness_on_external_resource_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["measure", "--resource", "TCPIP0::127.0.0.1::1::SOCKET", "--out", str(tmp_path / "x.csv")]
    )
    assert code == 2
    assert "thickness" in capsys.readouterr().err
