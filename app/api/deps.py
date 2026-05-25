"""FastAPI dependencies for the auth subsystem."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, WebSocket, status

from app.services.auth_service import ACCESS_COOKIE_NAME, decode_access_token
from app.storage.postgres.users_repo import User, get_users_repo


async def optional_user(request: Request) -> Optional[User]:
    """Return the current User or None. Never raises."""
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        return None
    claims = decode_access_token(token)
    if not claims:
        return None
    try:
        return get_users_repo().get_user(claims.user_id)
    except Exception:
        return None


async def require_user(user: Optional[User] = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"ok": False, "code": "UNAUTHORIZED", "message": "未登录或登录已过期"},
        )
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"ok": False, "code": "USER_INACTIVE", "message": "账号已被停用"},
        )
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    """For endpoints that must remain admin-only (monitor, system config)."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"ok": False, "code": "FORBIDDEN", "message": "仅管理员可访问"},
        )
    return user


async def require_user_ws(ws: WebSocket) -> Optional[User]:
    """Variant for WebSocket: returns User or None — caller decides whether to close."""
    token = ws.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        return None
    claims = decode_access_token(token)
    if not claims:
        return None
    try:
        u = get_users_repo().get_user(claims.user_id)
    except Exception:
        return None
    if u is None or u.status != "active":
        return None
    return u
