"""Phase 2 acceptance against a running ``docker compose up`` stack.

Run with ``MH_E2E_URL=http://localhost:8000 pytest tests/e2e``; skipped otherwise.
"""

from __future__ import annotations

import csv
import io
import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

BASE_URL = os.environ.get("MH_E2E_URL")
USERNAME = os.environ.get("MH_E2E_USERNAME", "operator")
PASSWORD = os.environ.get("MH_E2E_PASSWORD", "operator-dev-password")
SIMULATOR_RESOURCE = os.environ.get("MH_E2E_RESOURCE", "TCPIP0::simulator::5025::SOCKET")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(BASE_URL is None, reason="MH_E2E_URL is not set"),
]

# true values of the compose simulator: eps = 10 - 0.5j, mu = 1.5 - 0.2j, d = 3 mm
TRUTH = {"eps_re_mean": 10.0, "eps_im_mean": 0.5, "mu_re_mean": 1.5, "mu_im_mean": 0.2}


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    assert BASE_URL is not None
    with httpx.Client(base_url=f"{BASE_URL}/api/v1", timeout=30) as client:
        token = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        token.raise_for_status()
        client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        yield client


def test_job_through_the_stack(api: httpx.Client) -> None:
    suffix = uuid.uuid4().hex[:8]
    instrument = api.post(
        "/instruments",
        json={"name": f"sim-{suffix}", "resource": SIMULATOR_RESOURCE, "driver": "sim_vna"},
    )
    method = api.post(
        "/methods",
        json={"name": f"nrw-{suffix}", "params": {"points": 901, "segment_size": 50}},
    )
    sample = api.post(
        "/samples", json={"code": f"F-{suffix}", "material": "ferrite", "thickness_mm": 3.0}
    )
    for response in (instrument, method, sample):
        assert response.status_code == 201, response.text

    checked = api.post(f"/instruments/{instrument.json()['id']}/check").json()
    assert checked["status"] == "online"

    created = api.post(
        "/jobs",
        json={
            "instrument_id": instrument.json()["id"],
            "method_id": method.json()["id"],
            "sample_id": sample.json()["id"],
        },
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    deadline = time.monotonic() + 120
    job = created.json()
    while job["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.5)
        job = api.get(f"/jobs/{job_id}").json()
    assert job["status"] == "completed", job
    assert job["points_done"] == 901

    summary = api.get(f"/jobs/{job_id}/result").json()["summary"]
    for key, expected in TRUTH.items():
        assert abs(summary[key] - expected) / expected < 0.02, (key, summary[key])

    exported = api.get(f"/jobs/{job_id}/export", params={"format": "csv"})
    assert exported.status_code == 200
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[0][:5] == ["freq_hz", "eps_re", "eps_im", "mu_re", "mu_im"]
    assert len(rows) == 902
