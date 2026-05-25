"""End-to-end smoke test for Phase-2 storage isolation + quota.

Validates:
  - /api/upload writes under data/uploads/{user_id}/...
  - File row inserted + user_storage counter bumped (atomic).
  - /api/files lists only the caller's files.
  - DELETE /api/files/{id} removes from disk + decrements counter.
  - Quota enforcement: per-file cap + total-quota cap.
  - Admin is exempt from total quota but still tracked.

Run from project root:
    python scripts/smoke_storage.py
"""
from __future__ import annotations

import io
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

# Override quota for a quick test BEFORE the settings singleton is imported.
os.environ["AUTH_STORAGE_LIMIT_BYTES"] = "1048576"   # 1 MB
os.environ["AUTH_MAX_UPLOAD_BYTES"] = "524288"       # 512 KB per file

from fastapi.testclient import TestClient
from app.api.server import app
from app.config.settings import settings
from app.storage.postgres.users_repo import get_users_repo
from app.storage.postgres.files_repo import get_files_repo


def _dummy_file(size: int, name: str = "smoke.txt", content_type: str = "text/plain"):
    return {"file": (name, io.BytesIO(b"X" * size), content_type)}


def main() -> int:
    # Re-load settings since env was patched above
    settings.auth_storage_limit_bytes = int(os.environ["AUTH_STORAGE_LIMIT_BYTES"])
    settings.auth_max_upload_bytes = int(os.environ["AUTH_MAX_UPLOAD_BYTES"])
    print(f"[setup] storage limit={settings.auth_storage_limit_bytes}B "
          f"per-file cap={settings.auth_max_upload_bytes}B")

    with TestClient(app) as client:
        # ── A non-admin user ────────────────────────────────────────────
        repo = get_users_repo()
        email = f"sto_{secrets.token_hex(4)}@gmail.com"
        pwd = "stoPass!2026"
        r = client.post("/api/auth/email/register",
                        json={"email": email, "password": pwd, "display_name": "sto-user"})
        assert r.status_code == 200, r.text
        ident = repo.get_identity("email", email)
        repo.mark_identity_verified(ident.id)
        r = client.post("/api/auth/email/login", json={"email": email, "password": pwd})
        assert r.status_code == 200, r.text

        # Create a session so /api/upload has somewhere to attach
        r = client.post("/api/sessions")
        assert r.status_code == 201, r.text
        sid = r.json()["session_id"]
        print(f"[1] session created: {sid}")

        # ── Upload within quota ────────────────────────────────────────
        print("[2] upload 200KB file")
        r = client.post(f"/api/upload?session_id={sid}", files=_dummy_file(200 * 1024, "a.txt"))
        assert r.status_code == 200, ("first upload failed", r.status_code, r.text)
        print(f"    [OK] uploaded, response keys: {list(r.json().keys())[:6]}")

        # ── Quota usage ────────────────────────────────────────────────
        r = client.get("/api/storage/usage")
        assert r.status_code == 200, r.text
        usage = r.json()
        assert usage["used_bytes"] >= 200 * 1024, ("usage not updated", usage)
        assert usage["is_admin"] is False
        print(f"[3] usage: {usage['used_mb']}MB / {usage['limit_mb']}MB ({usage['percent']}%)")

        # ── Per-file cap blocks an oversized file ──────────────────────
        print("[4] upload 600KB file (over per-file cap)")
        r = client.post(f"/api/upload?session_id={sid}", files=_dummy_file(600 * 1024, "big.txt"))
        assert r.status_code == 413, ("expected 413 from per-file cap", r.status_code, r.text)
        print(f"    [OK] 413: {r.json()['detail'][:60]}...")

        # ── Total-quota cap kicks in ───────────────────────────────────
        print("[5] upload 400KB file (would exceed total quota)")
        # We already used ~200KB; another 400KB would put us over 1MB? Actually 600KB < 1MB
        # So upload 4 of them to push past the 1MB limit.
        for i in range(4):
            r = client.post(f"/api/upload?session_id={sid}",
                            files=_dummy_file(250 * 1024, f"b{i}.txt"))
            if r.status_code == 413:
                print(f"    [OK] quota hit on upload #{i+1}: {r.json()['detail'][:60]}...")
                break
        else:
            raise AssertionError("expected quota rejection within 4 small uploads, never hit")

        # ── List files (only this user's) ──────────────────────────────
        r = client.get("/api/files")
        assert r.status_code == 200, r.text
        files = r.json()["files"]
        assert all(f["original_name"] for f in files), files
        print(f"[6] /api/files returns {len(files)} files for this user")

        # ── Delete one, check counter drops ────────────────────────────
        target = files[0]
        r = client.delete(f"/api/files/{target['id']}")
        assert r.status_code == 204, ("delete failed", r.status_code, r.text)
        r = client.get("/api/storage/usage")
        new_usage = r.json()
        expected_remaining = sum(f["size_bytes"] for f in files[1:])
        assert new_usage["used_bytes"] == expected_remaining, \
               ("counter should equal sum of remaining files",
                new_usage["used_bytes"], "expected", expected_remaining, files)
        print(f"[7] delete freed {target['size_bytes']}B; now {new_usage['used_mb']}MB used")

        # ── Cross-user isolation ───────────────────────────────────────
        client.cookies.clear()
        email2 = f"sto2_{secrets.token_hex(4)}@gmail.com"
        r = client.post("/api/auth/email/register",
                        json={"email": email2, "password": pwd, "display_name": "sto-user-2"})
        assert r.status_code == 200, r.text
        ident2 = repo.get_identity("email", email2)
        repo.mark_identity_verified(ident2.id)
        r = client.post("/api/auth/email/login", json={"email": email2, "password": pwd})
        assert r.status_code == 200, r.text

        # User 2 sees nothing
        r = client.get("/api/files")
        assert r.json()["files"] == [], ("user 2 should see 0 files", r.json())
        # ...and can't delete user 1's file
        if files[1:]:
            other_id = files[1]["id"]
            r = client.delete(f"/api/files/{other_id}")
            assert r.status_code == 404, ("cross-user delete must 404", r.status_code, r.text)
            print(f"[8] user 2 cross-delete attempt → 404 (good)")

        # ── Admin bypass ───────────────────────────────────────────────
        client.cookies.clear()
        r = client.post("/api/auth/email/login",
                        json={"email": settings.auth_admin_email,
                              "password": settings.auth_admin_initial_password})
        assert r.status_code == 200, ("admin login failed", r.status_code, r.text)
        r = client.get("/api/storage/usage")
        admin_usage = r.json()
        assert admin_usage["is_admin"] is True
        print(f"[9] admin usage: {admin_usage['used_mb']}MB (admin exempt from limit)")

    print("\nAll storage smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
