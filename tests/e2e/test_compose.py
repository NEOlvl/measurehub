"""Acceptance tests against a running ``docker compose up`` stack.

Run with ``MH_E2E_URL=http://localhost:8000 pytest tests/e2e``; skipped otherwise.

The crash test also needs the ``docker`` CLI and is meant to run with a fast watchdog
(``MH_WATCHDOG_STALE_S=10 MH_WATCHDOG_INTERVAL_S=5`` when the stack is started); with the
defaults (60 s / 30 s) it still passes, it just takes about two minutes.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("MH_E2E_URL")
USERNAME = os.environ.get("MH_E2E_USERNAME", "operator")
PASSWORD = os.environ.get("MH_E2E_PASSWORD", "operator-dev-password")
SIM_RESOURCE = os.environ.get("MH_E2E_RESOURCE", "TCPIP0::simulator::5025::SOCKET")
SIM_HOST = os.environ.get("MH_E2E_SIM_HOST", "localhost")
SIM_PORT = int(os.environ.get("MH_E2E_SIM_PORT", "5025"))
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(BASE_URL is None, reason="MH_E2E_URL is not set"),
]

# (thickness mm, (eps', eps''), (mu', mu'')): three sample parameter sets (MH-47)
SAMPLES = [
    (3.0, (10.0, 0.5), (1.5, 0.2)),
    (4.0, (4.0, 0.1), (1.0, 0.05)),
    (2.0, (15.0, 1.5), (2.5, 0.8)),
]
DEMO = SAMPLES[0]


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    assert BASE_URL is not None
    with httpx.Client(base_url=f"{BASE_URL}/api/v1", timeout=30) as client:
        token = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        token.raise_for_status()
        client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        yield client


def scpi(*commands: str) -> None:
    """Send commands to the simulator published on the host (no answers expected)."""
    with socket.create_connection((SIM_HOST, SIM_PORT), timeout=10) as sock:
        sock.sendall(("\n".join(commands) + "\n").encode())
        time.sleep(0.2)


def set_simulator_sample(sample: tuple[float, tuple[float, float], tuple[float, float]]) -> None:
    thickness_mm, (eps_re, eps_loss), (mu_re, mu_loss) = sample
    scpi(
        f"SIM:SAMP:EPS {eps_re},{eps_loss}",
        f"SIM:SAMP:MU {mu_re},{mu_loss}",
        f"SIM:SAMP:THIC {thickness_mm / 1000}",
    )


@pytest.fixture
def simulator_reset() -> Iterator[None]:
    yield
    set_simulator_sample(DEMO)


def create_setup(api: httpx.Client, thickness_mm: float, params: dict[str, Any]) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    created = {
        "instrument": api.post(
            "/instruments",
            json={"name": f"sim-{suffix}", "resource": SIM_RESOURCE, "driver": "sim_vna"},
        ),
        "method": api.post("/methods", json={"name": f"nrw-{suffix}", "params": params}),
        "sample": api.post(
            "/samples",
            json={"code": f"S-{suffix}", "material": "ferrite", "thickness_mm": thickness_mm},
        ),
    }
    for response in created.values():
        assert response.status_code == 201, response.text
    return {
        "instrument_id": created["instrument"].json()["id"],
        "method_id": created["method"].json()["id"],
        "sample_id": created["sample"].json()["id"],
    }


def wait_for(
    api: httpx.Client, job_id: str, done: set[str], timeout_s: float = 120
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        job: dict[str, Any] = api.get(f"/jobs/{job_id}").json()
        if job["status"] in done or time.monotonic() > deadline:
            return job
        time.sleep(0.5)


def assert_recovered(summary: dict[str, float], sample: tuple[Any, ...]) -> None:
    _, (eps_re, eps_im), (mu_re, mu_im) = sample
    truth = {"eps_re_mean": eps_re, "eps_im_mean": eps_im, "mu_re_mean": mu_re, "mu_im_mean": mu_im}
    for key, expected in truth.items():
        assert abs(summary[key] - expected) / expected < 0.02, (key, summary[key], expected)


def test_job_through_the_stack(api: httpx.Client) -> None:
    ids = create_setup(api, 3.0, {"points": 901, "segment_size": 50})

    checked = api.post(f"/instruments/{ids['instrument_id']}/check").json()
    assert checked["status"] == "online"

    created = api.post("/jobs", json=ids)
    assert created.status_code == 201
    job_id = created.json()["id"]

    job = wait_for(api, job_id, {"completed", "failed"})
    assert job["status"] == "completed", job
    assert job["points_done"] == 901

    assert_recovered(api.get(f"/jobs/{job_id}/result").json()["summary"], DEMO)

    exported = api.get(f"/jobs/{job_id}/export", params={"format": "csv"})
    assert exported.status_code == 200
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[0][:5] == ["freq_hz", "eps_re", "eps_im", "mu_re", "mu_im"]
    assert len(rows) == 902


@pytest.mark.usefixtures("simulator_reset")
@pytest.mark.parametrize("sample", SAMPLES, ids=["ferrite-A", "dielectric-B", "ferrite-C"])
def test_eps_and_mu_are_recovered_for_three_samples(
    api: httpx.Client, sample: tuple[float, tuple[float, float], tuple[float, float]]
) -> None:
    set_simulator_sample(sample)
    ids = create_setup(api, sample[0], {"points": 1001, "segment_size": 100})

    job = api.post("/jobs", json=ids).json()
    job = wait_for(api, job["id"], {"completed", "failed"})

    assert job["status"] == "completed", job
    summary = api.get(f"/jobs/{job['id']}/result").json()["summary"]
    assert summary["flagged_fraction"] == 0
    assert_recovered(summary, sample)


def test_cancel_and_retry_through_the_stack(api: httpx.Client) -> None:
    # ifbw 100 Hz: a 50-point segment takes 0.5 s, the whole sweep about 10 s
    params = {"points": 1001, "segment_size": 50, "ifbw_hz": 100}
    ids = create_setup(api, 3.0, params)
    job = api.post("/jobs", json=ids).json()

    deadline = time.monotonic() + 60
    while api.get(f"/jobs/{job['id']}").json()["points_done"] < 100:
        assert time.monotonic() < deadline, "the job made no progress"
        time.sleep(0.2)

    assert api.post(f"/jobs/{job['id']}/cancel").status_code == 200
    cancelled = wait_for(api, job["id"], {"cancelled", "completed", "failed"})
    assert cancelled["status"] == "cancelled"
    assert 100 <= cancelled["points_done"] < 1001
    assert cancelled["points_done"] % 50 == 0  # whole segments only

    retried = api.post(f"/jobs/{job['id']}/retry")
    assert retried.status_code == 201
    assert retried.json()["params"] == job["params"]
    api.post(f"/jobs/{retried.json()['id']}/cancel")  # do not leave a 10 s job running
    wait_for(api, retried.json()["id"], {"cancelled", "completed", "failed"})


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI")
def test_killed_worker_job_is_resumed_by_the_watchdog(api: httpx.Client) -> None:
    params = {"points": 1001, "segment_size": 50, "ifbw_hz": 100}
    ids = create_setup(api, 3.0, params)
    job = api.post("/jobs", json=ids).json()

    deadline = time.monotonic() + 60
    while api.get(f"/jobs/{job['id']}").json()["points_done"] < 100:
        assert time.monotonic() < deadline, "the job made no progress"
        time.sleep(0.2)

    def compose(*args: str) -> None:
        subprocess.run(["docker", "compose", *args], cwd=REPO_ROOT, check=True)

    compose("kill", "worker")  # SIGKILL: no cleanup, the heartbeat stops
    try:
        killed = api.get(f"/jobs/{job['id']}").json()
        assert killed["status"] in {"running", "completed"}
        saved = killed["points_done"]
    finally:
        compose("up", "-d", "worker")

    # the watchdog re-queues the job, a worker resumes it from the first unsaved segment
    job = wait_for(api, job["id"], {"completed", "failed", "cancelled"}, timeout_s=300)
    assert job["status"] == "completed", job
    assert job["points_done"] == 1001
    assert job["attempt"] >= 1 or saved == 1001

    raw = api.get(f"/jobs/{job['id']}/export", params={"kind": "raw"})
    rows = list(csv.reader(io.StringIO(raw.text)))[1:]
    freqs = [float(row[0]) for row in rows]
    assert len(rows) == 1001  # nothing lost ...
    assert len(set(freqs)) == 1001  # ... and nothing duplicated
    assert freqs == sorted(freqs)
    assert_recovered(api.get(f"/jobs/{job['id']}/result").json()["summary"], DEMO)


def test_events_reach_a_websocket_client_within_a_second(api: httpx.Client) -> None:
    """NFR-03 on the real stack: worker -> Redis -> API -> WebSocket client."""
    from datetime import UTC, datetime

    from websockets.sync.client import connect

    assert BASE_URL is not None
    params = {"points": 301, "segment_size": 50, "ifbw_hz": 100}  # about 3 s
    ids = create_setup(api, 3.0, params)
    job = api.post("/jobs", json=ids).json()
    token = str(api.headers["Authorization"]).split()[1]
    ws_url = BASE_URL.replace("http", "ws", 1) + f"/ws/jobs/{job['id']}?token={token}"

    delays: list[float] = []
    kinds: set[str] = set()
    with connect(ws_url, open_timeout=10) as ws:
        first = json.loads(ws.recv(timeout=10))
        assert first["type"] == "snapshot"
        while True:
            message = json.loads(ws.recv(timeout=30))
            received = datetime.now(UTC)
            sent = datetime.fromisoformat(message["ts"].replace("Z", "+00:00"))
            delays.append((received - sent).total_seconds())
            kinds.add(message["type"])
            if message["type"] == "status" and message["status"] in {"completed", "failed"}:
                assert message["status"] == "completed"
                break

    assert {"segment", "progress", "status"} <= kinds
    assert max(delays) < 1.0, delays
