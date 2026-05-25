"""End-to-end smoke test for Phase 4c (reading) + 4d (profile / llm-config / monitor).

Validates:
  - Reading annotations + progress are user-isolated.
  - Profile is per-user (admin and non-admin keep different display_name etc.).
  - llm-config is readable by anyone but only admin can write.
  - /monitor/* requires admin.
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
        assert r.status_code == 200, ("admin login", r.status_code, r.text)
        admin_cookies = dict(client.cookies)
        admin_id = client.get("/api/me").json()["user"]["id"]

        # ── Admin sets a profile and reads it back ─────────────────────
        r = client.put("/api/profile", json={
            "display_name": "Admin Sunny", "self_description": "I run the system"
        })
        assert r.status_code == 200, r.text
        admin_profile = r.json()["profile"]
        assert admin_profile["display_name"] == "Admin Sunny"
        print(f"[1] admin profile saved: '{admin_profile['display_name']}'")

        # ── Admin annotates a doc ──────────────────────────────────────
        doc = f"doc_{secrets.token_hex(3)}"
        r = client.post("/api/reading/annotations", json={
            "doc_id": doc, "selected_text": "admin highlight",
            "rects": [{"x": 0, "y": 0, "w": 10, "h": 10}],
            "page": 1, "color": "yellow",
        })
        assert r.status_code == 200, r.text
        admin_ann_id = r.json()["annotation"]["id"]
        client.post("/api/reading/progress", json={"doc_id": doc, "page": 5, "scale": 1.2})

        # ── Admin can hit /monitor/summary ─────────────────────────────
        r = client.get("/monitor/summary?days=7")
        assert r.status_code == 200, ("admin monitor", r.status_code, r.text)
        print("[2] admin can read /monitor/summary")

        # ── Register a non-admin and log them in ───────────────────────
        client.cookies.clear()
        u_email = f"rdr_{secrets.token_hex(4)}@gmail.com"
        u_pwd = "rdrPass!2026"
        r = client.post("/api/auth/email/register",
                        json={"email": u_email, "password": u_pwd, "display_name": "rdr"})
        assert r.status_code == 200, r.text
        repo.mark_identity_verified(repo.get_identity("email", u_email).id)
        r = client.post("/api/auth/email/login", json={"email": u_email, "password": u_pwd})
        assert r.status_code == 200, r.text
        u_id = client.get("/api/me").json()["user"]["id"]
        print(f"[3] non-admin: {u_email} ({u_id})")

        # ── Non-admin's profile is independent ─────────────────────────
        r = client.get("/api/profile")
        assert r.status_code == 200, r.text
        # New user shouldn't inherit admin's display_name
        assert r.json()["profile"]["display_name"] != "Admin Sunny", \
               ("profile leakage", r.json())
        client.put("/api/profile", json={
            "display_name": "Reader Bob", "self_description": "PhD student"
        })
        assert client.get("/api/profile").json()["profile"]["display_name"] == "Reader Bob"
        print("[4] [OK] non-admin's profile is independent ('Reader Bob')")

        # Switch back to admin, confirm profile NOT overwritten
        client.cookies.clear()
        client.cookies.update(admin_cookies)
        assert client.get("/api/profile").json()["profile"]["display_name"] == "Admin Sunny"
        print("[5] [OK] admin profile still 'Admin Sunny' (not clobbered)")

        # ── Reading isolation: non-admin can't see admin's annotation ─
        client.cookies.clear()
        client.post("/api/auth/email/login", json={"email": u_email, "password": u_pwd})

        r = client.get(f"/api/reading/annotations?doc_id={doc}")
        assert r.status_code == 200, r.text
        anns = r.json()["annotations"]
        assert all(a.get("user_id") == u_id for a in anns), ("ann leakage", anns)
        assert not any(a.get("id") == admin_ann_id for a in anns)
        print(f"[6] [OK] non-admin sees 0 of admin's annotations on doc {doc}")

        # Non-admin can NOT update or delete admin's annotation
        r = client.patch(f"/api/reading/annotations/{admin_ann_id}", json={"note": "hack"})
        assert r.status_code == 404, ("cross-user patch must 404", r.status_code)
        r = client.delete(f"/api/reading/annotations/{admin_ann_id}")
        assert r.status_code == 404, ("cross-user delete must 404", r.status_code)
        print("[7] [OK] non-admin cross-user PATCH + DELETE both → 404")

        # Reading progress: non-admin has their own row
        r = client.get(f"/api/reading/progress?doc_id={doc}")
        assert r.status_code == 200, r.text
        prog = r.json()["progress"]
        assert prog == {} or prog.get("user_id") == u_id, ("progress leakage", prog)
        client.post("/api/reading/progress", json={"doc_id": doc, "page": 99, "scale": 0.8})
        assert client.get(f"/api/reading/progress?doc_id={doc}").json()["progress"]["page"] == 99
        print(f"[8] [OK] non-admin progress is independent (page=99)")

        # Back to admin: their progress untouched (page should still be 5)
        client.cookies.clear()
        client.cookies.update(admin_cookies)
        r = client.get(f"/api/reading/progress?doc_id={doc}")
        assert r.json()["progress"]["page"] == 5, ("admin progress clobbered!", r.json())
        print(f"[9] [OK] admin progress still page=5")

        # ── /monitor admin-only ────────────────────────────────────────
        client.cookies.clear()
        client.post("/api/auth/email/login", json={"email": u_email, "password": u_pwd})
        for path in ("/monitor/summary", "/monitor/daily-trend", "/monitor/agents",
                     "/monitor/errors"):
            r = client.get(path)
            assert r.status_code == 403, (f"non-admin should be 403 on {path}", r.status_code)
        print("[10] [OK] non-admin gets 403 on all /monitor/* endpoints")

        # ── llm-config: read works, write must 403 ─────────────────────
        r = client.get("/api/llm-config")
        assert r.status_code == 200, r.text
        assert r.json()["writable"] is False, "non-admin writable flag wrong"
        r = client.put("/api/llm-config", json={"config": {}})
        assert r.status_code == 403, ("non-admin write llm-config should be 403", r.status_code)
        print("[11] [OK] llm-config read=allowed, write=403")

        # ── Admin can write llm-config ─────────────────────────────────
        client.cookies.clear()
        client.cookies.update(admin_cookies)
        r = client.get("/api/llm-config")
        assert r.json()["writable"] is True
        print("[12] [OK] admin sees writable=true")

        # Cleanup
        r = client.delete(f"/api/reading/annotations/{admin_ann_id}")
        assert r.status_code == 204
        print("[13] cleanup done")

    print("\nAll Phase 4c+4d smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
