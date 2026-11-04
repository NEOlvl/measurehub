import pytest

from simulator.scpi import ScpiEngine


def ask(engine: ScpiEngine, line: str) -> list[str | None]:
    return [r.text for r in engine.process_line(line)]


def test_fault_commands_set_and_query() -> None:
    engine = ScpiEngine(seed=0)
    for command, value in (("DROP", "0.25"), ("HANG", "0.5"), ("ERR", "0.125"), ("LAT", "40")):
        ask(engine, f"SIM:FAULT:{command} {value}")
        assert ask(engine, f"SIM:FAULT:{command}?") == [value]
    for command in ("DROP", "HANG", "ERR"):
        ask(engine, f"SIM:FAULT:{command} 0")
    assert ask(engine, "SYST:ERR?") == ['0,"No error"']


@pytest.mark.parametrize(
    "command", ["SIM:FAULT:DROP 1.5", "SIM:FAULT:HANG -0.1", "SIM:FAULT:LAT -1"]
)
def test_fault_arguments_are_range_checked(command: str) -> None:
    engine = ScpiEngine()
    ask(engine, command)
    assert (ask(engine, "SYST:ERR?")[0] or "").startswith("-222")


def test_drop_closes_the_connection() -> None:
    engine = ScpiEngine(fault_drop=1.0, fault_seed=0)
    (reply,) = engine.process_line("*IDN?")
    assert reply.drop
    assert engine.faults_injected["drop"] == 1


def test_hang_swallows_the_line() -> None:
    engine = ScpiEngine(fault_hang=1.0, fault_seed=0)
    assert engine.process_line("*IDN?") == []
    assert engine.faults_injected["hang"] == 1


def test_err_queues_an_error_and_keeps_query_in_lockstep() -> None:
    engine = ScpiEngine(fault_err=1.0, fault_seed=0)
    assert ask(engine, "*IDN?") == [""]
    assert ask(engine, "SENS:SWE:POIN 7") == []
    engine.fault_err = 0.0
    assert ask(engine, "SENS:SWE:POIN?") == ["901"]  # the set command was not executed
    assert ask(engine, "SYST:ERR?") == ['-300,"Device-specific error"']


def test_fault_commands_are_never_faulted() -> None:
    engine = ScpiEngine(fault_drop=1.0, fault_seed=0)
    assert ask(engine, "SIM:FAULT:DROP 0") == []
    assert ask(engine, "*IDN?")[0] is not None


def test_latency_is_added_to_every_answer() -> None:
    engine = ScpiEngine(fault_latency_ms=250)
    (reply,) = engine.process_line("*IDN?")
    assert reply.delay_s == pytest.approx(0.25)


def test_fault_rate_follows_the_probability_and_is_reproducible() -> None:
    def run() -> int:
        engine = ScpiEngine(fault_drop=0.1, fault_seed=42)
        for _ in range(2000):
            engine.process_line("*IDN?")
        return engine.faults_injected["drop"]

    assert run() == run()
    assert 150 < run() < 250
