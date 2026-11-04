"""Samples (materials under test)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from measurehub.api.deps import (
    CurrentUser,
    PagingParams,
    Session,
    commit_or_conflict,
    get_or_404,
    paginate,
)
from measurehub.db.models import Sample
from measurehub.schemas.api import Page, SampleCreate, SampleOut, SampleUpdate

router = APIRouter(prefix="/samples", tags=["samples"])


@router.get("")
async def list_samples(
    session: Session, user: CurrentUser, paging: PagingParams
) -> Page[SampleOut]:
    return await paginate(session, select(Sample).order_by(Sample.code), paging, SampleOut)


@router.post("", status_code=201)
async def create_sample(body: SampleCreate, session: Session, user: CurrentUser) -> SampleOut:
    sample = Sample(**body.model_dump(), created_by=user.id)
    session.add(sample)
    await commit_or_conflict(session, f"Sample {body.code!r} already exists")
    return SampleOut.model_validate(sample)


@router.get("/{sample_id}")
async def get_sample(sample_id: uuid.UUID, session: Session, user: CurrentUser) -> SampleOut:
    return SampleOut.model_validate(await get_or_404(session, Sample, sample_id, "Sample"))


@router.patch("/{sample_id}")
async def update_sample(
    sample_id: uuid.UUID, body: SampleUpdate, session: Session, user: CurrentUser
) -> SampleOut:
    sample = await get_or_404(session, Sample, sample_id, "Sample")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(sample, field, value)
    await session.commit()
    return SampleOut.model_validate(sample)
