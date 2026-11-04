"""Job lifecycle through the API: create -> (inline) worker -> result -> export."""

from __future__ import annotations

import csv
import io
import json

import httpx
import pytest
from fastapi import FastAPI

from simulator.server import SimulatorThread
from tests.conftest import SIM_SAMPLE, make_fixtures

API = "/api/v1"


def _rel(value: float, expected: float) -> float:
    return abs(value - expected) / expected


async def test_job_runs_and_result_matches_the_sample(
    client: httpx.AsyncClient, sim: SimulatorThread
) -> None:
    ids = await make_fixtures(client, sim.port)
    created = await client.post(f"{API}/jobs", json=ids)
    assert created.status_code == 201
    job_id = created.json()["id"]
    assert created.json()["points_total"] == 201
    assert created.json()["segments_total"] == 5

    job = (await client.get(f"{API}/jobs/{job_id}")).json()
    assert job["status"] == "completed", job
    assert job["points_done"] == 201
    assert job["segments_done"] == 5
    assert job["started_at"]
    assert job["finished_at"]
    assert job["heartbeat_at"]
    assert job["error_code"] is None

    result = (await client.get(f"{API}/jobs/{job_id}/result")).json()
    assert result["processor"] == "nrw"
    summary = result["summary"]
    assert _rel(summary["eps_re_mean"], SIM_SAMPLE.eps.real) < 0.02
    assert _rel(summary["eps_im_mean"], -SIM_SAMPLE.eps.imag) < 0.02
    assert _rel(summary["mu_re_mean"], SIM_SAMPLE.mu.real) < 0.02
    assert _rel(summary["mu_im_mean"], -SIM_SAMPLE.mu.imag) < 0.02
    assert summary["flagged_fraction"] == 0
    assert len(result["data"]["freq_hz"]) == 201
    assert set(result["data"]["columns"]) == {"eps_re", "eps_im", "mu_re", "mu_im"}


async def test_export_result_csv_and_json(client: httpx.AsyncClient, sim: SimulatorThread) -> None:
    ids = await make_fixtures(client, sim.port)
    job_id = (await client.post(f"{API}/jobs", json=ids)).json()["id"]

    response = await client.get(f"{API}/jobs/{job_id}/export", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert f"job-{job_id}-result.csv" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == [
        "freq_hz", "eps_re", "eps_im", "mu_re", "mu_im", "phase_ambiguity", "invalid",
    ]  # fmt: skip
    assert len(rows) == 202
    assert float(rows[1][0]) == pytest.approx(1e9)
    assert float(rows[-1][0]) == pytest.approx(10e9)
    assert rows[1][-2:] == ["0", "0"]

    body = (await client.get(f"{API}/jobs/{job_id}/export", params={"format": "json"})).json()
    assert body["job_id"] == job_id
    assert body["summary"]["eps_re_mean"] == pytest.approx(10, rel=0.02)
    assert len(body["data"]["freq_hz"]) == 201


async def test_export_raw_points(client: httpx.AsyncClient, sim: SimulatorThread) -> None:
    ids = await make_fixtures(client, sim.port)
    job_id = (await client.post(f"{API}/jobs", json=ids)).json()["id"]

    as_csv = await client.get(f"{API}/jobs/{job_id}/export", params={"kind": "raw"})
    rows = list(csv.reader(io.StringIO(as_csv.text)))
    assert rows[0] == ["freq_hz", "s11_re", "s11_im", "s21_re", "s21_im"]
    assert len(rows) == 202

    as_json = (
        await client.get(f"{API}/jobs/{job_id}/export", params={"kind": "raw", "format": "json"})
    ).json()
    assert len(as_json["points"]["s21_re"]) == 201
    assert json.dumps(as_json)  # plain JSON, no NaN


async def test_raw_method_needs_no_sample(client: httpx.AsyncClient, sim: SimulatorThread) -> None:
    ids = await make_fixtures(client, sim.port, processor="raw")
    ids.pop("sample_id")
    job = (await client.post(f"{API}/jobs", json=ids)).json()
    assert (await client.get(f"{API}/jobs/{job['id']}")).json()["status"] == "completed"
    result = (await client.get(f"{API}/jobs/{job['id']}/result")).json()
    assert set(result["data"]["columns"]) == {"s11_db", "s11_deg", "s21_db", "s21_deg"}


async def test_params_override_changes_the_sweep(
    client: httpx.AsyncClient, sim: SimulatorThread
) -> None:
    ids = await make_fixtures(client, sim.port)
    created = await client.post(
        f"{API}/jobs", json=ids | {"params": {"points": 120, "segment_size": 40}}
    )
    assert created.json()["points_total"] == 120
    assert created.json()["segments_total"] == 3
    assert created.json()["params"]["start_hz"] == 1e9
    job = (await client.get(f"{API}/jobs/{created.json()['id']}")).json()
    assert job["points_done"] == 120


async def test_job_creation_validation(client: httpx.AsyncClient, sim: SimulatorThread) -> None:
    ids = await make_fixtures(client, sim.port)
    nil = "00000000-0000-0000-0000-000000000000"

    no_sample = await client.post(
        f"{API}/jobs", json={k: v for k, v in ids.items() if k != "sample_id"}
    )
    assert no_sample.status_code == 422
    assert "requires a sample" in no_sample.json()["detail"]

    bad_override = await client.post(f"{API}/jobs", json=ids | {"params": {"points": 99999}})
    assert bad_override.status_code == 422
    assert bad_override.json()["errors"][0]["loc"] == ["points"]

    for key in ("method_id", "instrument_id", "sample_id"):
        assert (await client.post(f"{API}/jobs", json=ids | {key: nil})).status_code == 404

    await client.patch(f"{API}/instruments/{ids['instrument_id']}", json={"is_enabled": False})
    assert (await client.post(f"{API}/jobs", json=ids)).status_code == 409
    await client.patch(f"{API}/instruments/{ids['instrument_id']}", json={"is_enabled": True})
    await client.patch(f"{API}/methods/{ids['method_id']}", json={"is_archived": True})
    assert (await client.post(f"{API}/jobs", json=ids)).status_code == 409


async def test_unreachable_instrument_fails_the_job(client: httpx.AsyncClient) -> None:
    ids = await make_fixtures(client, 1)  # nothing listens on port 1
    created = await client.post(f"{API}/jobs", json=ids)
    job = (await client.get(f"{API}/jobs/{created.json()['id']}")).json()
    assert job["status"] == "failed"
    assert job["error_code"] == "instrument_connection"
    assert job["finished_at"]
    result = await client.get(f"{API}/jobs/{job['id']}/result")
    assert result.status_code == 409
    export = await client.get(f"{API}/jobs/{job['id']}/export")
    assert export.status_code == 409


async def test_queued_job_has_no_result_yet(
    client: httpx.AsyncClient, sim: SimulatorThread, app: FastAPI
) -> None:
    ids = await make_fixtures(client, sim.port)
    app.state.dispatcher = lambda job_id: "task-1"
    job = (await client.post(f"{API}/jobs", json=ids)).json()
    assert job["status"] == "queued"
    again = (await client.get(f"{API}/jobs/{job['id']}")).json()
    assert again["status"] == "queued"
    assert again["points_done"] == 0
    assert (await client.get(f"{API}/jobs/{job['id']}/result")).status_code == 409


async def test_dispatch_failure_marks_job_failed_and_returns_503(
    client: httpx.AsyncClient, sim: SimulatorThread, app: FastAPI
) -> None:
    ids = await make_fixtures(client, sim.port)

    def broken(job_id: object) -> str:
        raise ConnectionError("broker down")

    app.state.dispatcher = broken
    response = await client.post(f"{API}/jobs", json=ids)
    assert response.status_code == 503
    jobs = (await client.get(f"{API}/jobs")).json()["items"]
    assert [j["status"] for j in jobs] == ["failed"]
    assert jobs[0]["error_code"] == "dispatch_failed"


async def test_job_list_filters_and_paging(
    client: httpx.AsyncClient, sim: SimulatorThread, app: FastAPI
) -> None:
    ids = await make_fixtures(client, sim.port)
    done = (await client.post(f"{API}/jobs", json=ids)).json()
    app.state.dispatcher = lambda job_id: None
    queued = [(await client.post(f"{API}/jobs", json=ids)).json() for _ in range(2)]

    everything = (await client.get(f"{API}/jobs")).json()
    assert everything["total"] == 3
    assert everything["items"][0]["id"] == queued[1]["id"]  # newest first

    by_status = (await client.get(f"{API}/jobs", params={"status": "completed"})).json()
    assert [j["id"] for j in by_status["items"]] == [done["id"]]
    assert (await client.get(f"{API}/jobs", params={"status": "queued"})).json()["total"] == 2
    assert (await client.get(f"{API}/jobs", params={"status": "bogus"})).status_code == 422

    by_instrument = await client.get(f"{API}/jobs", params={"instrument_id": ids["instrument_id"]})
    assert by_instrument.json()["total"] == 3
    other = await client.get(
        f"{API}/jobs", params={"instrument_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert other.json()["total"] == 0
    by_sample = await client.get(f"{API}/jobs", params={"sample_id": ids["sample_id"]})
    assert by_sample.json()["total"] == 3

    page = (await client.get(f"{API}/jobs", params={"limit": 1, "offset": 2})).json()
    assert page["total"] == 3
    assert [j["id"] for j in page["items"]] == [done["id"]]

    future = await client.get(f"{API}/jobs", params={"created_from": "2999-01-01T00:00:00"})
    assert future.json()["total"] == 0
    past = await client.get(f"{API}/jobs", params={"created_to": "2000-01-01T00:00:00"})
    assert past.json()["total"] == 0
