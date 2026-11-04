"""Real PyVISA (pyvisa-py backend) against the simulator over TCP."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
import pyvisa

from measurehub.drivers.errors import InstrumentError
from measurehub.drivers.models import Segment, VnaSweepParams
from measurehub.drivers.sim_vna import SimVnaDriver
from measurehub.drivers.sweep import run_sweep
from measurehub.drivers.transport import VisaTransport
from simulator.model import Sample, slab_sparameters
from simulator.scpi import ScpiEngine
from simulator.server import SimulatorThread

SAMPLE = Sample.from_parts(10, 0.5, 1.5, 0.2, 0.003)


@pytest.fixture
def sim() -> Iterator[SimulatorThread]:
    engine = ScpiEngine(SAMPLE, seed=7, noise_k=0.0, time_scale=0.0)
    with SimulatorThread(engine) as thread:
        yield thread


@pytest.fixture
def driver(sim: SimulatorThread) -> Iterator[SimVnaDriver]:
    drv = SimVnaDriver(VisaTransport(f"TCPIP0::127.0.0.1::{sim.port}::SOCKET", timeout_ms=3000))
    drv.connect()
    yield drv
    drv.close()


def test_raw_pyvisa_idn_and_901_point_sweep(sim: SimulatorThread) -> None:
    rm = pyvisa.ResourceManager("@py")
    inst: Any = rm.open_resource(f"TCPIP0::localhost::{sim.port}::SOCKET")
    inst.read_termination = "\n"
    inst.write_termination = "\n"
    try:
        assert inst.query("*IDN?") == "MeasureHub,SIM-VNA-01,000001,1.0"
        inst.write("INIT:IMM")
        assert inst.query("*OPC?").strip() == "1"
        freq = np.array(inst.query("SENS:FREQ:DATA?").split(","), dtype=float)
        data = np.array(inst.query("CALC:DATA:SDAT?").split(","), dtype=float)
    finally:
        inst.close()
        rm.close()
    assert freq.size == 901
    assert data.size == 4 * 901


def test_single_query_with_10001_points_survives_large_response(driver: SimVnaDriver) -> None:
    t = driver._transport
    t.write("SENS:SWE:POIN 10001")
    t.write("INIT:IMM")
    raw = t.query("CALC:DATA:SDAT?")
    assert len(raw.split(",")) == 4 * 10_001


def test_identify(driver: SimVnaDriver) -> None:
    assert driver.identify().model == "SIM-VNA-01"


def test_sweep_of_10001_points_in_segments_of_50_matches_the_model(driver: SimVnaDriver) -> None:
    params = VnaSweepParams(points=10_001, segment_size=50)
    seen: list[int] = []
    sweep = run_sweep(driver, params, on_segment=lambda d, total: seen.append(d.segment.index))
    assert seen == list(range(201))
    assert sweep.freq_hz.size == sweep.s11.size == sweep.s21.size == 10_001
    expected_s11, expected_s21 = slab_sparameters(sweep.freq_hz, SAMPLE)
    assert np.allclose(sweep.s11, expected_s11, atol=1e-8)
    assert np.allclose(sweep.s21, expected_s21, atol=1e-8)


def test_instrument_error_is_raised_as_domain_exception(driver: SimVnaDriver) -> None:
    driver.configure(VnaSweepParams())
    bad = Segment(index=0, first_idx=0, count=2, start_hz=5e9, stop_hz=1e9)  # start > stop
    with pytest.raises(InstrumentError) as info:
        driver.measure_segment(bad)
    assert info.value.code == -221
