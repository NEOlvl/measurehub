from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from simulator.server import SimulatorThread
from tests.conftest import make_fixtures

API = "/api/v1"
INSTRUMENT = {"name": "vna-1", "resource": "TCPIP0::127.0.0.1::1::SOCKET", "driver": "sim_vna"}


# -- instruments ------------------------------------------------------------------------------


async def test_instrument_crud(client: httpx.AsyncClient) -> None:
    created = await client.post(f"{API}/instruments", json=INSTRUMENT | {"model": "SIM"})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "offline"
    assert body["kind"] == "vna"
    assert body["is_enabled"] is True

    url = f"{API}/instruments/{body['id']}"
    assert (await client.get(url)).json()["name"] == "vna-1"
    patched = await client.patch(url, json={"name": "vna-2", "is_enabled": False})
    assert patched.json()["name"] == "vna-2"
    assert patched.json()["is_enabled"] is False
    assert (await client.get(f"{API}/instruments")).json()["total"] == 1
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404


async def test_instrument_validation_and_conflicts(client: httpx.AsyncClient) -> None:
    assert (await client.post(f"{API}/instruments", json=INSTRUMENT)).status_code == 201
    assert (await client.post(f"{API}/instruments", json=INSTRUMENT)).status_code == 409
    unknown = await client.post(
        f"{API}/instruments", json=INSTRUMENT | {"name": "x", "driver": "nope"}
    )
    assert unknown.status_code == 422
    assert "sim_vna" in unknown.json()["detail"]
    assert (await client.get(f"{API}/drivers")).json() == ["sim_vna"]


async def test_instrument_check_online_stores_idn(
    client: httpx.AsyncClient, sim: SimulatorThread
) -> None:
    created = await client.post(
        f"{API}/instruments",
        json=INSTRUMENT | {"resource": f"TCPIP0::127.0.0.1::{sim.port}::SOCKET"},
    )
    checked = await client.post(f"{API}/instruments/{created.json()['id']}/check")
    assert checked.status_code == 200
    assert checked.json()["status"] == "online"
    assert checked.json()["idn"] == "MeasureHub,SIM-VNA-01,000001,1.0"
    assert checked.json()["last_seen_at"] is not None


async def test_instrument_check_offline(client: httpx.AsyncClient) -> None:
    created = await client.post(f"{API}/instruments", json=INSTRUMENT)
    checked = await client.post(f"{API}/instruments/{created.json()['id']}/check")
    assert checked.json()["status"] == "offline"
    assert checked.json()["idn"] is None


async def test_instrument_with_unfinished_jobs_cannot_be_deleted(
    client: httpx.AsyncClient, sim: SimulatorThread, app: FastAPI
) -> None:
    ids = await make_fixtures(client, sim.port)
    # queue a job without running it
    app.state.dispatcher = lambda job_id: None
    job = await client.post(f"{API}/jobs", json=ids)
    assert job.json()["status"] == "queued"
    response = await client.delete(f"{API}/instruments/{ids['instrument_id']}")
    assert response.status_code == 409


# -- samples ----------------------------------------------------------------------------------


async def test_sample_crud(client: httpx.AsyncClient) -> None:
    payload = {"code": "F-1", "material": "ferrite", "thickness_mm": 2.5, "description": "d"}
    created = await client.post(f"{API}/samples", json=payload)
    assert created.status_code == 201
    sample = created.json()
    assert sample["thickness_mm"] == 2.5

    url = f"{API}/samples/{sample['id']}"
    patched = await client.patch(url, json={"thickness_mm": 3.0})
    assert patched.json()["thickness_mm"] == 3.0
    assert patched.json()["material"] == "ferrite"
    assert (await client.get(url)).json()["code"] == "F-1"
    assert (await client.post(f"{API}/samples", json=payload)).status_code == 409
    assert (
        await client.get(f"{API}/samples/00000000-0000-0000-0000-000000000000")
    ).status_code == 404


async def test_sample_list_is_paginated(client: httpx.AsyncClient) -> None:
    for i in range(5):
        await client.post(
            f"{API}/samples", json={"code": f"S-{i}", "material": "m", "thickness_mm": 1}
        )
    page = (await client.get(f"{API}/samples", params={"limit": 2, "offset": 3})).json()
    assert page["total"] == 5
    assert [s["code"] for s in page["items"]] == ["S-3", "S-4"]


# -- methods ----------------------------------------------------------------------------------


async def test_method_crud_with_defaults_filled_in(client: httpx.AsyncClient) -> None:
    created = await client.post(f"{API}/methods", json={"name": "m1", "params": {"points": 101}})
    assert created.status_code == 201
    method = created.json()
    assert method["processor"] == "nrw"
    assert method["params"]["points"] == 101
    assert method["params"]["segment_size"] == 50  # default filled in

    url = f"{API}/methods/{method['id']}"
    patched = await client.patch(url, json={"params": {"points": 51}, "processor": "raw"})
    assert patched.json()["params"]["points"] == 51
    assert patched.json()["processor"] == "raw"

    await client.patch(url, json={"is_archived": True})
    assert (await client.get(f"{API}/methods")).json()["total"] == 0
    assert (await client.get(f"{API}/methods", params={"include_archived": True})).json()[
        "total"
    ] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad", "params": {"points": 1}},
        {"name": "bad", "params": {"start_hz": 5e9, "stop_hz": 1e9}},
        {"name": "bad", "params": {"unknown": 1}},
        {"name": "bad", "processor": "nope"},
        {"name": "bad", "measurement_type": "nope"},
    ],
)
async def test_method_validation(client: httpx.AsyncClient, body: dict[str, object]) -> None:
    response = await client.post(f"{API}/methods", json=body)
    assert response.status_code == 422, response.text


async def test_method_duplicate_name_conflicts(client: httpx.AsyncClient) -> None:
    await client.post(f"{API}/methods", json={"name": "dup"})
    assert (await client.post(f"{API}/methods", json={"name": "dup"})).status_code == 409


async def test_parameter_json_schema(client: httpx.AsyncClient) -> None:
    schema = (await client.get(f"{API}/measurement-types/vna_sweep/schema")).json()
    assert schema["properties"]["points"]["maximum"] == 10_001
    assert schema["properties"]["points"]["minimum"] == 2
    assert set(schema["properties"]) >= {"start_hz", "stop_hz", "ifbw_hz", "segment_size"}
    assert (await client.get(f"{API}/measurement-types/nope/schema")).status_code == 422
