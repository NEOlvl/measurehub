"""FastAPI dependencies: database session, current user, paging."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from measurehub.api.errors import ApiError, conflict, not_found
from measurehub.core.security import decode_access_token
from measurehub.core.settings import Settings
from measurehub.db.base import Base
from measurehub.db.models import User
from measurehub.schemas.api import Page

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _unauthorized(detail: str) -> ApiError:
    return ApiError(401, "Unauthorized", detail, headers={"WWW-Authenticate": "Bearer"})


async def current_user(
    request: Request,
    session: Session,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None:
        raise _unauthorized("Missing bearer token")
    try:
        subject = decode_access_token(
            credentials.credentials, request.app.state.settings.secret_key
        )
        user = await session.get(User, uuid.UUID(subject))
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized("Invalid or expired token") from None
    if user is None or not user.is_active:
        raise _unauthorized("Unknown or inactive user")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


@dataclass(frozen=True, slots=True)
class Paging:
    limit: int
    offset: int


def _paging(
    limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0
) -> Paging:
    return Paging(limit, offset)


PagingParams = Annotated[Paging, Depends(_paging)]


async def get_or_404[M: Base](
    session: AsyncSession, model: type[M], key: uuid.UUID, name: str
) -> M:
    obj = await session.get(model, key)
    if obj is None:
        raise not_found(name, key)
    return obj


async def paginate(
    session: AsyncSession, stmt: Select[Any], paging: Paging, schema: type[Any]
) -> Page[Any]:
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.scalars(stmt.limit(paging.limit).offset(paging.offset))).all()
    return Page(items=[schema.model_validate(row) for row in rows], total=total or 0)


async def commit_or_conflict(session: AsyncSession, detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise conflict(detail) from None
