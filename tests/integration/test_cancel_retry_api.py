"""POST /jobs/{id}/cancel and POST /jobs/{id}/retry."""

from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from measurehub.core.states import JobStatus
from measurehub.db.models import Job
from measurehub.worker.cancel import cancel_key
from simulator.server import SimulatorThread
from tests.conftest import make_fixtures

API = "/api/v1"
NIL = "00000000-0000-0000-0000-000000000000"


def set_status(factory: sessionmaker[Session], job_id: str, status: JobStatus) -> None:
    with factory() as session:
        job = session.get(Job, uuid.UUID(job_id))
        assert job is not None
        job.status = status
        session.commit()


async def queued_job(
    client: httpx.AsyncClient, app: FastAPI, sim: SimulatorThread
) -> tuple[dict[str, str], dict[str, object]]:
    ids = await make_fixtures(client, sim.port)
    app.state.dispatcher = lambda job_id: "task"
    return ids, (await client.post(f"{API}/jobs", json=ids)).json()


async def test_cancel_queued_job_is_immediate(
    client: httpx.AsyncClient, app: FastAPI, sim: SimulatorThread
) -> None:
    _, job = await queued_job(client, app, sim)
    response = await client.post(f"{API}/jobs/{job['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["finished_at"]
    assert (await client.post(f"{API}/jobs/{job['id']}/cancel")).json()["status"] == "cancelled"


async def test_cancel_running_job_sets_cancelling_and_the_redis_flag(
    client: httpx.AsyncClient,
    app: FastAPI,
    sim: SimulatorThread,
    session_factory: sessionmaker[Session],
) -> None:
    _, job = await queued_job(client, app, sim)
    set_status(session_factory, str(job["id"]), JobStatus.RUNNING)

    response = await client.post(f"{API}/jobs/{job['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    assert await app.state.redis.exists(cancel_key(uuid.UUID(str(job["id"])))) == 1
    again = await client.post(f"{API}/jobs/{job['id']}/cancel")
    assert again.json()["status"] == "cancelling"  # idempotent


async def test_jobs_that_cannot_be_cancelled_get_409(
    client: httpx.AsyncClient, sim: SimulatorThread
) -> None:
    ids = await make_fixtures(client, sim.port)
    done = (await client.post(f"{API}/jobs", json=ids)).json()  # runs inline -> completed
    response = await client.post(f"{API}/jobs/{done['id']}/cancel")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "completed" in response.json()["detail"]


async def test_cancel_unknown_job_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.post(f"{API}/jobs/{NIL}/cancel")).status_code == 404
    assert (await client.post(f"{API}/jobs/{NIL}/retry")).status_code == 404


async def test_retry_of_a_cancelled_job_creates_a_new_job_with_the_same_parameters(
    client: httpx.AsyncClient, app: FastAPI, sim: SimulatorThread
) -> None:
    ids, job = await queued_job(client, app, sim)
    await client.post(f"{API}/jobs/{job['id']}/cancel")
    sent: list[uuid.UUID] = []

    def dispatch(job_id: uuid.UUID) -> str:
        sent.append(job_id)
        return "task"

    app.state.dispatcher = dispatch

    response = await client.post(f"{API}/jobs/{job['id']}/retry")

    assert response.status_code == 201
    new = response.json()
    assert new["id"] != job["id"]
    assert new["status"] == "queued"
    assert (new["method_id"], new["instrument_id"], new["sample_id"]) == (
        ids["method_id"],
        ids["instrument_id"],
        ids["sample_id"],
    )
    assert new["params"] == job["params"]
    assert sent == [uuid.UUID(new["id"])]
    # the original job is untouched
    assert (await client.get(f"{API}/jobs/{job['id']}")).json()["status"] == "cancelled"


async def test_retry_of_a_failed_job_runs_it_again(
    client: httpx.AsyncClient, sim: SimulatorThread, session_factory: sessionmaker[Session]
) -> None:
    ids = await make_fixtures(client, sim.port)
    job = (await client.post(f"{API}/jobs", json=ids)).json()
    set_status(session_factory, job["id"], JobStatus.FAILED)

    new = (await client.post(f"{API}/jobs/{job['id']}/retry")).json()

    assert (await client.get(f"{API}/jobs/{new['id']}")).json()["status"] == "completed"


async def test_only_failed_or_cancelled_jobs_can_be_retried(
    client: httpx.AsyncClient, app: FastAPI, sim: SimulatorThread
) -> None:
    _, queued = await queued_job(client, app, sim)
    assert (await client.post(f"{API}/jobs/{queued['id']}/retry")).status_code == 409
