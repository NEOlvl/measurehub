import pytest

from simulator.model import Sample
from simulator.scpi import IDN, ScpiEngine


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def ask(engine: ScpiEngine, line: str) -> str | None:
    replies = engine.process_line(line)
    assert len(replies) <= 1
    return replies[0].text if replies else None


@pytest.fixture
def engine() -> ScpiEngine:
    return ScpiEngine(Sample(4 + 0j, 1 + 0j, 0.005), seed=0, time_scale=0.0)


def test_idn(engine: ScpiEngine) -> None:
    assert ask(engine, "*IDN?") == IDN


def test_set_and_query_with_long_and_short_forms(engine: ScpiEngine) -> None:
    ask(engine, "SENS:FREQ:STAR 2e9")
    ask(engine, "sense:frequency:stop 4e9")
    ask(engine, ":SENS:SWE:POIN 11")
    assert float(ask(engine, "SENS:FREQ:STAR?") or "") == 2e9
    assert float(ask(engine, "SENSe:FREQuency:STOP?") or "") == 4e9
    assert ask(engine, "SENS:SWE:POIN?") == "11"
    assert ask(engine, "SYST:ERR?") == '0,"No error"'


def test_rst_restores_defaults(engine: ScpiEngine) -> None:
    ask(engine, "SENS:SWE:POIN 11")
    ask(engine, "*RST")
    assert ask(engine, "SENS:SWE:POIN?") == "901"


def test_unknown_command_goes_to_error_queue(engine: ScpiEngine) -> None:
    assert ask(engine, "FOO:BAR 1") is None
    assert ask(engine, "SYST:ERR?") == '-113,"Undefined header"'
    assert ask(engine, "SYST:ERR?") == '0,"No error"'


def test_cls_clears_errors(engine: ScpiEngine) -> None:
    ask(engine, "FOO")
    ask(engine, "*CLS")
    assert ask(engine, "SYST:ERR?") == '0,"No error"'


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("SENS:SWE:POIN abc", "-104"),
        ("SENS:SWE:POIN 1.5", "-104"),
        ("SENS:SWE:POIN 0", "-222"),
        ("SENS:FREQ:STAR 1", "-222"),
        ("SENS:BWID", "-109"),
        ("SIM:SAMP:EPS 2", "-108"),
    ],
)
def test_invalid_arguments(engine: ScpiEngine, command: str, error: str) -> None:
    ask(engine, command)
    assert (ask(engine, "SYST:ERR?") or "").startswith(error)


def test_failed_query_answers_with_empty_line(engine: ScpiEngine) -> None:
    assert ask(engine, "CALC:DATA:SDAT?") == ""
    assert (ask(engine, "SYST:ERR?") or "").startswith("-230")


def test_start_above_stop_is_a_settings_conflict(engine: ScpiEngine) -> None:
    ask(engine, "SENS:FREQ:STAR 5e9")
    ask(engine, "SENS:FREQ:STOP 2e9")
    ask(engine, "INIT:IMM")
    assert (ask(engine, "SYST:ERR?") or "").startswith("-221")


def test_sweep_returns_frequencies_and_interleaved_sparameters(engine: ScpiEngine) -> None:
    ask(engine, "SENS:FREQ:STAR 1e9")
    ask(engine, "SENS:FREQ:STOP 3e9")
    ask(engine, "SENS:SWE:POIN 5")
    ask(engine, "INIT:IMM")
    assert ask(engine, "*OPC?") == "1"
    freq = [float(x) for x in (ask(engine, "SENS:FREQ:DATA?") or "").split(",")]
    assert freq == [1e9, 1.5e9, 2e9, 2.5e9, 3e9]
    data = (ask(engine, "CALC:DATA:SDAT?") or "").split(",")
    assert len(data) == 20  # (re, im) x 5 points x (S11, S21)


def test_sweep_time_is_points_times_averaging_over_ifbw_and_capped() -> None:
    clock = FakeClock()
    engine = ScpiEngine(seed=0, clock=clock, max_sweep_s=5.0)
    ask(engine, "SENS:SWE:POIN 500")
    ask(engine, "SENS:BWID 1000")
    ask(engine, "SENS:AVER:COUN 2")
    assert engine.sweep_time_s == pytest.approx(1.0)
    ask(engine, "SENS:SWE:POIN 10001")
    ask(engine, "SENS:AVER:COUN 64")
    assert engine.sweep_time_s == 5.0


def test_opc_waits_for_the_remaining_sweep_time() -> None:
    clock = FakeClock()
    engine = ScpiEngine(seed=0, clock=clock)
    ask(engine, "SENS:SWE:POIN 500")  # 0.5 s at 1000 Hz
    ask(engine, "INIT:IMM")
    clock.now = 0.2
    (reply,) = engine.process_line("*OPC?")
    assert reply.text == "1"
    assert reply.delay_s == pytest.approx(0.3)
    clock.now = 1.0
    (reply,) = engine.process_line("*OPC?")
    assert reply.delay_s == 0.0


def test_sample_commands_change_the_physics(engine: ScpiEngine) -> None:
    def s21_magnitude() -> float:
        ask(engine, "SENS:SWE:POIN 1")
        ask(engine, "SENS:FREQ:STAR 5e9")
        ask(engine, "SENS:FREQ:STOP 5e9")
        ask(engine, "INIT:IMM")
        values = [float(x) for x in (ask(engine, "CALC:DATA:SDAT?") or "").split(",")]
        return float(abs(complex(values[2], values[3])))

    ask(engine, "SIM:SAMP:EPS 1,0")
    ask(engine, "SIM:SAMP:MU 1,0")
    assert s21_magnitude() == pytest.approx(1.0, abs=0.02)
    ask(engine, "SIM:SAMP:EPS 10,5")
    ask(engine, "SIM:SAMP:THIC 0.02")
    assert s21_magnitude() < 0.5
    assert ask(engine, "SIM:SAMP:EPS?") == "10,5"
    assert float(ask(engine, "SIM:SAMP:THIC?") or "") == 0.02


def test_multiple_commands_on_one_line(engine: ScpiEngine) -> None:
    replies = engine.process_line("SENS:SWE:POIN 7;SENS:SWE:POIN?;*IDN?")
    assert [r.text for r in replies] == ["7", IDN]
