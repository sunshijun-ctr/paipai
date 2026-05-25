"""Auth HTTP endpoints — PR-1 covers email/password + JWT cookie session.

Routes (all under /api/auth except /api/me):

  POST   /api/auth/email/register   {email, password, display_name}
  GET    /api/auth/email/verify     ?token=...
  POST   /api/auth/email/login      {email, password}
  POST   /api/auth/email/forgot     {email}
  POST   /api/auth/email/reset      {token, new_password}

  POST   /api/auth/refresh          (uses ra_rt cookie)
  POST   /api/auth/logout
  DELETE /api/auth/identity/{id}    (must be authed; refuses last identity)

  GET    /api/me
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import optional_user, require_user
from app.config.settings import settings
from app.services import auth_service, email_service
from app.storage.postgres.users_repo import User, get_users_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])


# ── Schemas ───────────────────────────────────────────────────────────────

class EmailRegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)


class EmailLoginIn(BaseModel):
    email: EmailStr
    password: str


class EmailForgotIn(BaseModel):
    email: EmailStr


class EmailResetIn(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


# ── Cookie helpers ────────────────────────────────────────────────────────

def _set_session_cookies(response: Response, *, access: str, refresh: str) -> None:
    domain = settings.auth_cookie_domain or None
    common = dict(
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        domain=domain,
        path="/",
    )
    response.set_cookie(
        auth_service.ACCESS_COOKIE_NAME, access,
        max_age=settings.auth_access_token_ttl_minutes * 60,
        **common,
    )
    response.set_cookie(
        auth_service.REFRESH_COOKIE_NAME, refresh,
        max_age=settings.auth_refresh_token_ttl_days * 24 * 60 * 60,
        **common,
    )


def _clear_session_cookies(response: Response) -> None:
    domain = settings.auth_cookie_domain or None
    response.delete_cookie(auth_service.ACCESS_COOKIE_NAME, domain=domain, path="/")
    response.delete_cookie(auth_service.REFRESH_COOKIE_NAME, domain=domain, path="/")


def _client_meta(request: Request) -> tuple[Optional[str], Optional[str]]:
    ua = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    return ua, ip


def _err(code: str, message: str, http_status: int = 400) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"ok": False, "code": code, "message": message})


# ── Email register / activate ────────────────────────────────────────────

@router.post("/auth/email/register")
async def email_register(payload: EmailRegisterIn):
    repo = get_users_repo()
    email = payload.email.lower().strip()

    existing = repo.get_identity("email", email)
    if existing:
        # Already verified — collision; not verified — allow re-sending the email.
        if existing.verified_at is not None:
            raise _err("EMAIL_TAKEN", "该邮箱已被注册", http_status=409)
        user = repo.get_user(existing.user_id)
        if user is None:
            raise _err("INTERNAL", "账号状态异常，请联系管理员", http_status=500)
        # Re-issue activation; refresh password too if provided.
        repo.update_identity_credential(existing.id, auth_service.hash_password(payload.password))
        token = repo.issue_email_token(email=email, purpose="register",
                                       user_id=user.id, ttl_minutes=60)
        link = f"{settings.auth_public_base_url.rstrip('/')}/api/auth/email/verify?token={token}"
        subject, html, text = email_service.render_activation_email(
            display_name=user.display_name, link=link)
        await email_service.send_email(to=email, subject=subject, html=html, text=text)
        return {"ok": True, "message": "激活邮件已重新发送"}

    user = repo.create_user(display_name=payload.display_name, primary_email=email)
    repo.add_identity(
        user_id=user.id,
        provider="email",
        provider_uid=email,
        credential=auth_service.hash_password(payload.password),
        verified=False,
    )
    token = repo.issue_email_token(email=email, purpose="register",
                                   user_id=user.id, ttl_minutes=60)
    link = f"{settings.auth_public_base_url.rstrip('/')}/api/auth/email/verify?token={token}"
    subject, html, text = email_service.render_activation_email(
        display_name=payload.display_name, link=link)
    await email_service.send_email(to=email, subject=subject, html=html, text=text)
    return {"ok": True, "message": "注册成功，请到邮箱点击激活链接"}


@router.get("/auth/email/verify", response_class=HTMLResponse)
async def email_verify(token: str = Query(..., min_length=8)):
    """Activate the user's account and show a branded HTML success / failure page.

    Users land here from the email link, so this must render nicely in a
    browser — returning plain JSON would look terrible."""
    repo = get_users_repo()
    record = repo.consume_email_token(token, purpose="register")
    if not record:
        return HTMLResponse(_verify_page(success=False), status_code=400)
    email = record["email"]
    identity = repo.get_identity("email", email)
    if identity:
        repo.mark_identity_verified(identity.id)
    return HTMLResponse(_verify_page(success=True), status_code=200)


def _verify_page(*, success: bool) -> str:
    """Lavender-on-white activation result page matching the landing palette."""
    if success:
        title = "账号已激活"
        sub   = "你的 paipai 账号现在可以登录使用了。"
        icon  = """<svg width="56" height="56" viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <circle cx="12" cy="12" r="11" fill="#F0EBFB" stroke="#9B6FD4" stroke-width="1.5"/>
  <path d="M7 12.5L10.5 16L17 9" stroke="#534AB7" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""
        cta_label = "立即登录"
        cta_href  = "/#login"
    else:
        title = "链接无效或已过期"
        sub   = "激活链接可能已使用过、过期，或被复制截断。请回登录页重新发送激活邮件。"
        icon  = """<svg width="56" height="56" viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <circle cx="12" cy="12" r="11" fill="#FEE2E2" stroke="#DC2626" stroke-width="1.5"/>
  <path d="M8 8L16 16M16 8L8 16" stroke="#DC2626" stroke-width="2.4" stroke-linecap="round"/>
</svg>"""
        cta_label = "返回登录"
        cta_href  = "/#login"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · paipai</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#1A1530;background:
  radial-gradient(ellipse 80% 50% at 20% 0%, rgba(83,74,183,.14) 0%, transparent 60%),
  radial-gradient(ellipse 60% 40% at 90% 20%, rgba(155,111,212,.14) 0%, transparent 55%),
  radial-gradient(ellipse 70% 50% at 50% 100%, rgba(29,158,117,.08) 0%, transparent 60%),
  #FAF8FF;
  display:flex;align-items:center;justify-content:center;padding:32px 16px;line-height:1.6;-webkit-font-smoothing:antialiased}}
.card{{width:min(460px,100%);background:rgba(255,255,255,.92);border:1px solid rgba(83,74,183,.16);border-radius:22px;padding:38px 34px 30px;
  box-shadow:0 30px 80px rgba(83,74,183,.20);text-align:center;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}}
.brand{{display:inline-flex;align-items:center;gap:8px;margin-bottom:22px}}
.logo-svg{{animation:float 3.6s ease-in-out infinite;overflow:visible}}
.p-back{{font-family:system-ui,sans-serif;font-size:52px;font-weight:600;fill:url(#grad-back);opacity:.72;transform-origin:20px 44px;transition:transform .35s cubic-bezier(.34,1.56,.64,1),opacity .35s ease}}
.p-front{{font-family:system-ui,sans-serif;font-size:52px;font-weight:600;fill:url(#grad-front);opacity:.92;transform-origin:38px 38px;transition:transform .35s cubic-bezier(.34,1.56,.64,1) .04s,opacity .35s ease}}
.brand:hover .p-back{{transform:translate(-3px,2px) scale(1.06);opacity:.88}}
.brand:hover .p-front{{transform:translate(3px,-2px) scale(1.06);opacity:1}}
.brand:hover .logo-svg{{animation:none}}
@keyframes float{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-3px)}}}}
.wordmark{{font-family:system-ui,sans-serif;font-size:22px;font-weight:500;letter-spacing:.04em;
  background:linear-gradient(110deg,#534AB7 0%,#9B6FD4 50%,#1D9E75 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}}
.icon{{margin:6px auto 16px}}
h1{{font-size:22px;font-weight:700;letter-spacing:-0.01em;margin:4px 0 8px}}
.sub{{color:#6B5F8A;font-size:14px;margin-bottom:24px}}
.cta{{display:inline-block;padding:12px 28px;border-radius:11px;font-weight:700;font-size:14.5px;color:#fff;text-decoration:none;
  background:linear-gradient(135deg,#534AB7 0%,#3B348A 60%,#7B5FC0 100%);box-shadow:0 10px 24px rgba(83,74,183,.32);
  transition:transform .12s,box-shadow .2s}}
.cta:hover{{transform:translateY(-1px);box-shadow:0 14px 32px rgba(83,74,183,.40)}}
.foot{{margin-top:18px;font-size:12px;color:#6B5F8A}}
.foot a{{color:#534AB7;text-decoration:none}}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <linearGradient id="grad-back" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#AFA9EC"/><stop offset="100%" stop-color="#9B6FD4"/>
    </linearGradient>
    <linearGradient id="grad-front" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#534AB7"/><stop offset="60%" stop-color="#7B5FC0"/><stop offset="100%" stop-color="#1D9E75"/>
    </linearGradient>
  </defs>
</svg>

<div class="card">
  <a class="brand" href="/" aria-label="paipai home">
    <svg class="logo-svg" width="42" height="42" viewBox="0 0 68 68" xmlns="http://www.w3.org/2000/svg">
      <text class="p-back"  x="4"  y="54">p</text>
      <text class="p-front" x="22" y="46">p</text>
    </svg>
    <span class="wordmark">paipai</span>
  </a>

  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p class="sub">{sub}</p>
  <a class="cta" href="{cta_href}">{cta_label}</a>

  <p class="foot">回 <a href="/">paipai 首页</a></p>
</div>
</body>
</html>"""


# ── Email login ──────────────────────────────────────────────────────────

@router.post("/auth/email/login")
async def email_login(payload: EmailLoginIn, request: Request, response: Response):
    repo = get_users_repo()
    email = payload.email.lower().strip()
    identity = repo.get_identity("email", email)
    if not identity:
        raise _err("INVALID_CREDENTIAL", "邮箱或密码错误", http_status=401)
    if not auth_service.verify_password(payload.password, identity.credential or ""):
        raise _err("INVALID_CREDENTIAL", "邮箱或密码错误", http_status=401)
    if identity.verified_at is None:
        raise _err("EMAIL_NOT_VERIFIED", "邮箱尚未激活，请先到邮箱完成激活", http_status=403)

    user = repo.get_user(identity.user_id)
    if user is None or user.status != "active":
        raise _err("USER_INACTIVE", "账号不可用", http_status=403)

    ua, ip = _client_meta(request)
    access = auth_service.issue_access_token(user.id)
    refresh = auth_service.issue_refresh_token(user_id=user.id, user_agent=ua, ip=ip)
    _set_session_cookies(response, access=access, refresh=refresh)
    return {"ok": True, "user": _user_dto(user)}


# ── Forgot / reset ───────────────────────────────────────────────────────

@router.post("/auth/email/forgot")
async def email_forgot(payload: EmailForgotIn):
    repo = get_users_repo()
    email = payload.email.lower().strip()
    identity = repo.get_identity("email", email)
    # If the email isn't registered: silently return ok=True (don't leak that
    # the address doesn't exist). Rate limits only apply to real accounts —
    # we never issue tokens for non-existent emails, so they can't hit the cap.
    if not identity:
        return {"ok": True, "message": "如果邮箱已注册，重置链接已发送"}

    user = repo.get_user(identity.user_id)
    if user is None:
        return {"ok": True, "message": "如果邮箱已注册，重置链接已发现"}

    # ── Rate-limit gate ────────────────────────────────────────────────────
    # Tracks issuances by the token table itself, so the limits survive process
    # restarts without needing Redis.
    now = datetime.now(timezone.utc)
    cooldown_secs = settings.auth_reset_cooldown_seconds
    daily_limit   = settings.auth_reset_daily_limit

    recent_cooldown = repo.count_email_tokens_since(
        email=email, purpose="reset_password",
        since=now - timedelta(seconds=cooldown_secs),
    )
    if recent_cooldown > 0:
        mins = max(cooldown_secs // 60, 1)
        raise _err(
            "RESET_COOLDOWN",
            f"刚刚已经发过一封重置邮件，请 {mins} 分钟后再试",
            http_status=429,
        )

    recent_daily = repo.count_email_tokens_since(
        email=email, purpose="reset_password",
        since=now - timedelta(days=1),
    )
    if recent_daily >= daily_limit:
        raise _err(
            "RESET_DAILY_LIMIT",
            f"今天已重置 {daily_limit} 次，请明天再试",
            http_status=429,
        )

    # ── Issue token + send email ───────────────────────────────────────────
    token = repo.issue_email_token(email=email, purpose="reset_password",
                                   user_id=user.id, ttl_minutes=30)
    link = f"{settings.auth_public_base_url.rstrip('/')}/account.html#reset?token={token}"
    subject, html, text = email_service.render_reset_email(
        display_name=user.display_name, link=link)
    await email_service.send_email(to=email, subject=subject, html=html, text=text)
    return {"ok": True, "message": "重置链接已发送，请查收邮箱"}


@router.post("/auth/email/reset")
async def email_reset(payload: EmailResetIn):
    repo = get_users_repo()
    record = repo.consume_email_token(payload.token, purpose="reset_password")
    if not record:
        raise _err("INVALID_OR_EXPIRED", "重置链接无效或已过期", http_status=400)
    email = record["email"]
    identity = repo.get_identity("email", email)
    if not identity:
        raise _err("NOT_FOUND", "账号不存在", http_status=404)
    repo.update_identity_credential(identity.id, auth_service.hash_password(payload.new_password))
    # Force re-login on all sessions.
    repo.revoke_all_for_user(identity.user_id)
    return {"ok": True, "message": "密码已重置，请重新登录"}


# ── Refresh / logout ─────────────────────────────────────────────────────

@router.post("/auth/refresh")
async def refresh(request: Request, response: Response):
    old = request.cookies.get(auth_service.REFRESH_COOKIE_NAME)
    if not old:
        raise _err("UNAUTHORIZED", "缺少刷新凭证", http_status=401)
    ua, ip = _client_meta(request)
    rotated = auth_service.rotate_refresh_token(old, user_agent=ua, ip=ip)
    if not rotated:
        _clear_session_cookies(response)
        raise _err("UNAUTHORIZED", "刷新凭证已失效，请重新登录", http_status=401)
    new_access, new_refresh = rotated
    _set_session_cookies(response, access=new_access, refresh=new_refresh)
    return {"ok": True}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    rt = request.cookies.get(auth_service.REFRESH_COOKIE_NAME)
    if rt:
        auth_service.revoke_refresh_token(rt)
    _clear_session_cookies(response)
    return {"ok": True}


# ── Identity management ──────────────────────────────────────────────────

@router.delete("/auth/identity/{identity_id}", status_code=204)
async def unbind_identity(identity_id: str, user: User = Depends(require_user)):
    ok = get_users_repo().delete_identity(identity_id, user.id)
    if not ok:
        raise _err("LAST_IDENTITY", "至少需要保留一个登录方式", http_status=400)


# ── /api/me ──────────────────────────────────────────────────────────────

@router.get("/me")
async def me(user: Optional[User] = Depends(optional_user)):
    if user is None:
        raise _err("UNAUTHORIZED", "未登录", http_status=401)
    identities = get_users_repo().list_identities(user.id)
    return {
        "ok": True,
        "user": _user_dto(user),
        "identities": [
            {
                "id": i.id,
                "provider": i.provider,
                "provider_uid": _mask_provider_uid(i.provider, i.provider_uid),
                "verified": i.verified_at is not None,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in identities
        ],
    }


# ── Helpers ──────────────────────────────────────────────────────────────

def _user_dto(user: User) -> dict:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "primary_email": user.primary_email,
        "primary_phone": user.primary_phone,
        "is_admin": user.is_admin,
    }


# ── Storage quota + file management ──────────────────────────────────────

@router.get("/storage/usage")
async def storage_usage(user: User = Depends(require_user)):
    from app.services.storage_service import user_quota
    q = user_quota(user)
    return {
        "ok": True,
        "used_bytes":  q.used_bytes,
        "used_mb":     round(q.used_bytes / 1024 / 1024, 1),
        "limit_bytes": q.limit_bytes,
        "limit_mb":    round(q.limit_bytes / 1024 / 1024, 0),
        "free_bytes":  q.free_bytes,
        "free_mb":     round(q.free_bytes / 1024 / 1024, 1),
        "percent":     q.percent,
        "is_admin":    q.is_admin,
    }


@router.get("/files")
async def list_files(
    category: Optional[str] = None,
    user: User = Depends(require_user),
):
    from app.storage.postgres.files_repo import get_files_repo
    records = get_files_repo().list_by_user(user.id, category=category)
    return {
        "ok": True,
        "files": [
            {
                "id": r.id,
                "category": r.category,
                "original_name": r.original_name,
                "size_bytes": r.size_bytes,
                "mime_type": r.mime_type,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
    }


@router.delete("/files/{file_id}", status_code=204)
async def remove_file(file_id: str, user: User = Depends(require_user)):
    from app.services.storage_service import delete_file_by_id
    deleted = delete_file_by_id(user=user, file_id=file_id)
    if deleted is None:
        raise _err("NOT_FOUND", "文件不存在或无权访问", http_status=404)


def _mask_provider_uid(provider: str, uid: str) -> str:
    """Hide most of the email/phone for the /me payload."""
    if provider == "email":
        if "@" not in uid:
            return uid
        local, domain = uid.split("@", 1)
        if len(local) <= 2:
            return "*@" + domain
        return local[0] + "*" * (len(local) - 2) + local[-1] + "@" + domain
    if provider == "phone":
        return uid[:3] + "****" + uid[-4:] if len(uid) >= 7 else uid
    return uid
