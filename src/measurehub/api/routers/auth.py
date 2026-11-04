"""Authentication: login with username and password, access token only."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from measurehub.api.deps import CurrentUser, Session
from measurehub.api.errors import ApiError
from measurehub.core.security import create_access_token, verify_password
from measurehub.db.models import User
from measurehub.schemas.api import LoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(body: LoginRequest, session: Session, request: Request) -> TokenResponse:
    user = await session.scalar(select(User).where(User.username == body.username))
    valid = verify_password(body.password, user.password_hash if user else None)
    if user is None or not valid or not user.is_active:
        raise ApiError(
            401,
            "Unauthorized",
            "Wrong username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = request.app.state.settings
    token = create_access_token(str(user.id), settings.secret_key, settings.access_token_minutes)
    return TokenResponse(access_token=token, expires_in=settings.access_token_minutes * 60)


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
