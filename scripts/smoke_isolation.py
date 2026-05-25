"""End-to-end smoke test for Phase-1 multi-user data isolation.

Validates:
  - Admin bootstrap creates / promotes AUTH_ADMIN_EMAIL.
  - Existing session JSON files without owner_user_id get claimed by admin.
  - Admin sees their own sessions (incl. claimed legacy ones).
  - A second non-admin user cannot see or touch admin's sessions.
  - New sessions are stamped to the creator.

Run from project root:
    python scripts/smoke_isolation.py
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from fastapi.testclient import TestClient
from app.api.server import app
from app.config.settings import settings
from app.storage.postgres.users_repo import get_users_repo
from app.services import admin_bootstrap, auth_service


SESSIONS_DIR = os.path.join("data", "memory", "sessions")


def _seed_legacy_session() -> str:
    """Create an orphan session JSON to confirm migration claims it."""
    sid = f"smoke_{secrets.token_hex(4)}"
    path = os.path.join(SESSIONS_DIR, f"{sid}.json")
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "session_id": sid,
            "recent_turns": [{"role": "user", "content": "legacy question"}],
            "updated_at": "2026-05-01T00:00:00",
        }, f, ensure_ascii=False, indent=2)
    return sid


def main() -> int:
    print("[1] seeding a legacy orphan session")
    legacy_sid = _seed_legacy_session()
    print(f"    seeded: {legacy_sid}")

    print("[2] running admin bootstrap (claims orphans)")
    # Use TestClient as a context manager so FastAPI lifespan runs (orchestrator init etc.).
    # The lifespan also calls run_startup_bootstrap() — calling it once here too is safe (idempotent).
    admin_bootstrap.run_startup_bootstrap()
    admin_email = (settings.auth_admin_email or "").lower()
    repo = get_users_repo()
    admin = repo.get_user_by_email(admin_email)
    assert admin is not None, "admin user should exist after bootstrap"
    assert admin.is_admin, "admin must have is_admin=True"
    print(f"    admin user: {admin.id}  email: {admin.primary_email}  is_admin: {admin.is_admin}")

    # Verify legacy session was claimed
    with open(os.path.join(SESSIONS_DIR, legacy_sid + ".json"), encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("owner_user_id") == admin.id, ("legacy not claimed", data.get("owner_user_id"))
    print(f"    [OK] legacy session owner_user_id == admin.id")

    # Context manager triggers lifespan (orchestrator init, full bootstrap, etc.)
    with TestClient(app) as client:
        return _run_with_client(client, admin, repo, legacy_sid)


def _run_with_client(client, admin, repo, legacy_sid) -> int:
    admin_email = admin.primary_email

    print("[3] admin logs in with AUTH_ADMIN_INITIAL_PASSWORD")
    r = client.post("/api/auth/email/login",
                    json={"email": admin_email, "password": settings.auth_admin_initial_password})
    assert r.status_code == 200, ("admin login failed", r.status_code, r.text)
    admin_cookies = dict(r.cookies)
    me = client.get("/api/me").json()
    assert me["user"]["is_admin"] is True
    print(f"    [OK] /api/me reports is_admin=True")

    print("[4] admin GET /api/sessions  (must include the claimed legacy session)")
    r = client.get("/api/sessions")
    assert r.status_code == 200, r.text
    admin_sids = [s["session_id"] for s in r.json()["sessions"]]
    assert legacy_sid in admin_sids, ("admin should see legacy session", admin_sids)
    print(f"    [OK] admin sees {len(admin_sids)} sessions including {legacy_sid}")

    print("[5] register a second non-admin user")
    second_email = f"iso_{secrets.token_hex(4)}@gmail.com"
    second_pwd = "isoPass!2026"
    r = client.post("/api/auth/email/register",
                    json={"email": second_email, "password": second_pwd, "display_name": "iso-user"})
    assert r.status_code == 200, r.text
    # Bypass email activation
    iso_id = repo.get_identity("email", second_email)
    repo.mark_identity_verified(iso_id.id)
    print(f"    [OK] user {second_email} created")

    # Swap to the second user's cookie jar
    client.cookies.clear()
    r = client.post("/api/auth/email/login", json={"email": second_email, "password": second_pwd})
    assert r.status_code == 200, r.text
    print("[6] second user logs in")
    me2 = client.get("/api/me").json()
    assert me2["user"]["is_admin"] is False
    print(f"    user id: {me2['user']['id']}  is_admin: {me2['user']['is_admin']}")

    print("[7] second user GET /api/sessions  (must NOT see admin's sessions)")
    r = client.get("/api/sessions")
    iso_sids = [s["session_id"] for s in r.json()["sessions"]]
    assert legacy_sid not in iso_sids, ("isolation BROKEN — non-admin sees admin's session", iso_sids)
    print(f"    [OK] non-admin sees {len(iso_sids)} sessions (none of admin's)")

    print(f"[8] second user GET /api/sessions/{legacy_sid}  (must be 404)")
    r = client.get(f"/api/sessions/{legacy_sid}")
    assert r.status_code == 404, ("expected 404 for cross-user access", r.status_code, r.text)
    print(f"    [OK] 404 as expected")

    print(f"[9] second user DELETE /api/sessions/{legacy_sid}  (must be 404, admin's session untouched)")
    r = client.delete(f"/api/sessions/{legacy_sid}")
    assert r.status_code == 404, ("expected 404 on delete", r.status_code, r.text)
    assert os.path.exists(os.path.join(SESSIONS_DIR, legacy_sid + ".json")), \
        "admin's session file must still exist"
    print(f"    [OK] delete rejected, file still on disk")

    print("[10] second user creates a new session  (stamped with their user_id)")
    r = client.post("/api/sessions")
    assert r.status_code == 201, r.text
    new_sid = r.json()["session_id"]
    with open(os.path.join(SESSIONS_DIR, new_sid + ".json"), encoding="utf-8") as f:
        new_data = json.load(f)
    assert new_data.get("owner_user_id") == me2["user"]["id"]
    print(f"    [OK] new session {new_sid} stamped to non-admin user")

    print("[11] back to admin: admin can still see + delete the legacy session")
    client.cookies.clear()
    client.cookies.update(admin_cookies)
    r = client.delete(f"/api/sessions/{legacy_sid}")
    # 204 success
    assert r.status_code == 204, ("admin delete should succeed", r.status_code, r.text)
    print(f"    [OK] admin deleted legacy session")

    print("\nAll isolation smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
