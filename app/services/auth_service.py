"""Auth primitives: password hashing, JWT issuing/verifying, refresh-token rotation.

Stateless layer above UsersRepository. The router layer combines this with
cookie management to produce the full login flow.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from app.config.settings import settings
from app.storage.postgres.users_repo import User, get_users_repo

logger = logging.getLogger(__name__)

ACCESS_COOKIE_NAME = "ra_at"
REFRESH_COOKIE_NAME = "ra_rt"

# bcrypt has a hard 72-byte limit on the input. Truncating silently is the
# accepted workaround (the validator caps password length to 128 chars anyway).
_BCRYPT_MAX = 72


# ── Password hashing ──────────────────────────────────────────────────────

def _to_bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_bytes(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(password), hashed.encode("ascii"))
    except Exception:
        return False


# ── JWT access tokens ─────────────────────────────────────────────────────

@dataclass
class AccessTokenClaims:
    user_id: str
    expires_at: datetime


def issue_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=settings.auth_access_token_ttl_minutes)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": "access",
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm=settings.auth_jwt_algorithm)


def decode_access_token(token: str) -> Optional[AccessTokenClaims]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.auth_jwt_secret, algorithms=[settings.auth_jwt_algorithm])
    except JWTError:
        return None
    if payload.get("typ") != "access":
        return None
    user_id = payload.get("sub")
    exp = payload.get("exp")
    if not user_id or not exp:
        return None
    return AccessTokenClaims(user_id=str(user_id), expires_at=datetime.fromtimestamp(int(exp), tz=timezone.utc))


# ── Refresh tokens ────────────────────────────────────────────────────────

def issue_refresh_token(*, user_id: str, user_agent: Optional[str] = None,
                        ip: Optional[str] = None) -> str:
    """Generate, persist (hashed), and return an opaque refresh token."""
    token = secrets.token_urlsafe(48)  # ~64 chars, 384 bits
    repo = get_users_repo()
    repo.store_refresh_token(
        user_id=user_id,
        token=token,
        ttl_days=settings.auth_refresh_token_ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    return token


def rotate_refresh_token(old_token: str, *, user_agent: Optional[str] = None,
                         ip: Optional[str] = None) -> Optional[tuple[str, str]]:
    """Validate an existing refresh token, revoke it, and issue (access, refresh) pair.

    Returns None if the old token is invalid / expired / already revoked."""
    repo = get_users_repo()
    user_id = repo.consume_refresh_token(old_token)
    if not user_id:
        return None
    repo.revoke_refresh_token(old_token)
    new_access = issue_access_token(user_id)
    new_refresh = issue_refresh_token(user_id=user_id, user_agent=user_agent, ip=ip)
    return new_access, new_refresh


def revoke_refresh_token(token: str) -> None:
    if not token:
        return
    get_users_repo().revoke_refresh_token(token)


# ── Lookups ───────────────────────────────────────────────────────────────

def get_user_by_id(user_id: str) -> Optional[User]:
    return get_users_repo().get_user(user_id)
