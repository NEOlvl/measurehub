"""Methods (measurement presets) and parameter JSON Schemas."""

from __future__ import annotations

import uuid
from typing import Any

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
from measurehub.db.models import Method
from measurehub.schemas.api import MethodCreate, MethodOut, MethodUpdate, Page
from measurehub.services.methods import params_model, validate_params, validate_processor

router = APIRouter(tags=["methods"])


@router.get("/measurement-types/{measurement_type}/schema")
async def measurement_type_schema(measurement_type: str, user: CurrentUser) -> dict[str, Any]:
    return params_model(measurement_type).model_json_schema()


@router.get("/methods")
async def list_methods(
    session: Session, user: CurrentUser, paging: PagingParams, include_archived: bool = False
) -> Page[MethodOut]:
    stmt = select(Method).order_by(Method.name)
    if not include_archived:
        stmt = stmt.where(Method.is_archived.is_(False))
    return await paginate(session, stmt, paging, MethodOut)


@router.post("/methods", status_code=201)
async def create_method(body: MethodCreate, session: Session, user: CurrentUser) -> MethodOut:
    validate_processor(body.processor)
    method = Method(
        name=body.name,
        measurement_type=body.measurement_type,
        params=validate_params(body.measurement_type, body.params),
        processor=body.processor,
        created_by=user.id,
    )
    session.add(method)
    await commit_or_conflict(session, f"Method {body.name!r} already exists")
    return MethodOut.model_validate(method)


@router.get("/methods/{method_id}")
async def get_method(method_id: uuid.UUID, session: Session, user: CurrentUser) -> MethodOut:
    return MethodOut.model_validate(await get_or_404(session, Method, method_id, "Method"))


@router.patch("/methods/{method_id}")
async def update_method(
    method_id: uuid.UUID, body: MethodUpdate, session: Session, user: CurrentUser
) -> MethodOut:
    method = await get_or_404(session, Method, method_id, "Method")
    if body.params is not None:
        method.params = validate_params(method.measurement_type, body.params)
    if body.processor is not None:
        validate_processor(body.processor)
        method.processor = body.processor
    if body.is_archived is not None:
        method.is_archived = body.is_archived
    await commit_or_conflict(session, "Method update conflicts with an existing method")
    return MethodOut.model_validate(method)
