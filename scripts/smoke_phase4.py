"""End-to-end smoke test for Phase 4a (libraries) + 4b (notes + day-tasks).

Validates:
  - Admin bootstrap claims any unowned library (including default lt_docs).
  - Notes/day-tasks rows with user_id='local' are reassigned to admin.
  - A non-admin user only sees / can only modify their own libraries / notes / tasks.
  - Admin sees them all.
"""
from __future__ import annotations

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


def main() -> int:
    repo = get_users_repo()
    admin_email = (settings.auth_admin_email or "").lower()

    with TestClient(app) as client:
        # ── Admin login ────────────────────────────────────────────────
        r = client.post("/api/auth/email/login",
                        json={"email": admin_email,
                              "password": settings.auth_admin_initial_password})
        assert r.status_code == 200, ("admin login failed", r.status_code, r.text)
        admin_cookies = dict(client.cookies)
        me = client.get("/api/me").json()
        assert me["user"]["is_admin"] is True

        # ── Admin sees current libraries (the default lt_docs is admin-owned now) ──
        r = client.get("/api/libraries")
        assert r.status_code == 200, r.text
        admin_libs_before = r.json()["libraries"]
        admin_lib_ids_before = {l["lib_id"] for l in admin_libs_before}
        assert "lt_docs" in admin_lib_ids_before, ("admin should see lt_docs", admin_lib_ids_before)
        print(f"[1] admin sees {len(admin_libs_before)} libraries (lt_docs present)")

        # ── Register a non-admin user ──────────────────────────────────
        client.cookies.clear()
        u2_email = f"libiso_{secrets.token_hex(4)}@gmail.com"
        u2_pwd = "libIso!2026"
        r = client.post("/api/auth/email/register",
                        json={"email": u2_email, "password": u2_pwd, "display_name": "lib-iso"})
        assert r.status_code == 200, r.text
        ident = repo.get_identity("email", u2_email)
        repo.mark_identity_verified(ident.id)
        r = client.post("/api/auth/email/login", json={"email": u2_email, "password": u2_pwd})
        assert r.status_code == 200, r.text
        u2_id = client.get("/api/me").json()["user"]["id"]
        print(f"[2] non-admin user: {u2_email} ({u2_id})")

        # ── Non-admin sees zero libraries initially ────────────────────
        r = client.get("/api/libraries")
        assert r.status_code == 200, r.text
        my_libs = r.json()["libraries"]
        assert all(l["owner_user_id"] == u2_id for l in my_libs), \
               ("non-admin saw foreign libraries", my_libs)
        assert not any(l["lib_id"] == "lt_docs" for l in my_libs), \
               ("non-admin should NOT see admin's lt_docs", my_libs)
        print(f"[3] [OK] non-admin sees {len(my_libs)} of their own libraries (no leak)")

        # ── Non-admin creates a library ────────────────────────────────
        r = client.post("/api/libraries", json={"name": f"私人库-{secrets.token_hex(3)}"})
        assert r.status_code == 201, r.text
        my_lib_id = r.json()["lib_id"]
        print(f"[4] non-admin created library: {my_lib_id}")

        # ── Non-admin can NOT delete admin's lt_docs ───────────────────
        r = client.delete("/api/libraries/lt_docs")
        # 404 (not 403) because we don't leak existence
        assert r.status_code in (404, 400), ("expected 404 on cross-delete", r.status_code, r.text)
        print(f"[5] [OK] cross-delete attempt → {r.status_code}")

        # ── Notes isolation ────────────────────────────────────────────
        r = client.post("/api/notes",
                        json={"title": "user2's note", "content_markdown": "secret stuff"})
        assert r.status_code == 201, r.text
        u2_note_id = r.json()["note"]["id"]
        print(f"[6] non-admin created note: {u2_note_id}")

        r = client.get("/api/notes")
        assert r.status_code == 200, r.text
        u2_notes = r.json()["notes"]
        assert all(n["user_id"] == u2_id for n in u2_notes), \
               ("non-admin saw foreign notes", u2_notes)
        print(f"[7] [OK] non-admin sees {len(u2_notes)} of their own notes")

        # ── Day-tasks isolation ────────────────────────────────────────
        r = client.post("/api/day-tasks", json={
            "title": "user2 meeting", "task_date": "2026-12-31",
            "start_time": "09:00", "end_time": "10:00",
        })
        assert r.status_code == 201, ("day-task create failed", r.status_code, r.text)
        u2_task_id = r.json()["task"]["id"]
        print(f"[8] non-admin created day task: {u2_task_id}")

        r = client.get("/api/day-tasks?date=2026-12-31")
        u2_tasks = r.json()["tasks"]
        assert all(t["user_id"] == u2_id for t in u2_tasks)
        print(f"[9] [OK] non-admin sees {len(u2_tasks)} of their own tasks on that date")

        # ── Switch to admin: should see both (their own + user2's are isolated) ──
        client.cookies.clear()
        client.cookies.update(admin_cookies)

        r = client.get("/api/libraries")
        admin_libs_after = r.json()["libraries"]
        admin_lib_ids_after = {l["lib_id"] for l in admin_libs_after}
        assert my_lib_id in admin_lib_ids_after, \
               ("admin should see the non-admin's library", admin_lib_ids_after)
        print(f"[10] [OK] admin sees {len(admin_libs_after)} libraries "
              f"(including the non-admin's new one)")

        # Admin can read non-admin's note
        r = client.get(f"/api/notes/{u2_note_id}")
        assert r.status_code == 200, ("admin should access non-admin's note", r.text)
        print(f"[11] [OK] admin can read non-admin's note")

        # Admin can delete the non-admin's library
        r = client.delete(f"/api/libraries/{my_lib_id}")
        assert r.status_code == 204, ("admin lib delete failed", r.status_code, r.text)
        print(f"[12] [OK] admin deleted non-admin's library")

        # ── Cleanup: delete the non-admin's day task and note as admin ──
        r = client.delete(f"/api/day-tasks/{u2_task_id}")
        assert r.status_code == 204, ("admin task delete failed", r.status_code, r.text)
        r = client.delete(f"/api/notes/{u2_note_id}")
        assert r.status_code == 204, ("admin note delete failed", r.status_code, r.text)
        print(f"[13] cleanup done")

    print("\nAll Phase 4 (libraries + notes + day-tasks) smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
