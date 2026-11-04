"""Jobs: create, list, inspect, result and export."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from measurehub.api.deps import CurrentUser, PagingParams, Session, get_or_404, paginate
from measurehub.api.errors import conflict, not_found
from measurehub.core.states import JobStatus
from measurehub.db.models import Job, MeasurementPoint, Result
from measurehub.schemas.api import JobCreate, JobOut, Page, ResultOut
from measurehub.services.export import raw_csv, result_csv
from measurehub.services.jobs import create_job

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", status_code=201)
async def post_job(
    body: JobCreate, session: Session, user: CurrentUser, request: Request
) -> JobOut:
    job = await create_job(session, body, user, request.app.state.dispatcher)
    return JobOut.model_validate(job)


@router.get("")
async def list_jobs(
    session: Session,
    user: CurrentUser,
    paging: PagingParams,
    status: JobStatus | None = None,
    instrument_id: uuid.UUID | None = None,
    sample_id: uuid.UUID | None = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
) -> Page[JobOut]:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    if instrument_id is not None:
        stmt = stmt.where(Job.instrument_id == instrument_id)
    if sample_id is not None:
        stmt = stmt.where(Job.sample_id == sample_id)
    if created_from is not None:
        stmt = stmt.where(Job.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Job.created_at <= created_to)
    return await paginate(session, stmt, paging, JobOut)


@router.get("/{job_id}")
async def get_job(job_id: uuid.UUID, session: Session, user: CurrentUser) -> JobOut:
    return JobOut.model_validate(await get_or_404(session, Job, job_id, "Job"))


async def _current_result(session: Session, job: Job) -> Result:
    result = await session.scalar(
        select(Result).where(Result.job_id == job.id, Result.is_current.is_(True))
    )
    if result is None:
        raise conflict(f"Job is {job.status.value}: no result yet")
    return result


@router.get("/{job_id}/result")
async def get_result(job_id: uuid.UUID, session: Session, user: CurrentUser) -> ResultOut:
    job = await get_or_404(session, Job, job_id, "Job")
    return ResultOut.model_validate(await _current_result(session, job))


@router.get("/{job_id}/export")
async def export_job(
    job_id: uuid.UUID,
    session: Session,
    user: CurrentUser,
    format: Literal["csv", "json"] = "csv",
    kind: Literal["result", "raw"] = "result",
) -> Response:
    job = await get_or_404(session, Job, job_id, "Job")
    filename = f"job-{job.id}-{kind}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if kind == "result":
        result = await _current_result(session, job)
        if format == "csv":
            return Response(result_csv(result.data), media_type="text/csv", headers=headers)
        body: dict[str, Any] = {
            "job_id": str(job.id),
            "processor": result.processor,
            "summary": result.summary,
            "data": result.data,
        }
        return Response(json.dumps(body), media_type="application/json", headers=headers)

    rows = (
        await session.execute(
            select(
                MeasurementPoint.freq_hz,
                MeasurementPoint.s11_re,
                MeasurementPoint.s11_im,
                MeasurementPoint.s21_re,
                MeasurementPoint.s21_im,
            )
            .where(MeasurementPoint.job_id == job.id)
            .order_by(MeasurementPoint.idx)
        )
    ).all()
    if not rows:
        raise not_found("Raw points of job", job.id)
    if format == "csv":
        return Response(raw_csv(rows), media_type="text/csv", headers=headers)
    columns = {name: [row[i] for row in rows] for i, name in enumerate(rows[0]._fields)}
    return Response(
        json.dumps({"job_id": str(job.id), "points": columns}),
        media_type="application/json",
        headers=headers,
    )
