"""Password hashing (argon2) and access tokens (JWT)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("measurehub-dummy")
_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify in roughly constant time even when the user does not exist (hash is None)."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(subject: str, secret_key: str, minutes: int) -> str:
    now = datetime.now(UTC)
    payload = {"sub": subject, "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(payload, secret_key, algorithm=_ALGORITHM)


def decode_access_token(token: str, secret_key: str) -> str:
    """Return the subject; raises ``jwt.PyJWTError`` for invalid or expired tokens."""
    payload = jwt.decode(token, secret_key, algorithms=[_ALGORITHM], options={"require": ["exp"]})
    return str(payload["sub"])
