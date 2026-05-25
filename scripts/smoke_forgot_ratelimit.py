"""Smoke test the forgot-password rate limits.

Verifies:
  - 1st reset request is accepted (200 ok=True)
  - 2nd request within cooldown is rejected (429 RESET_COOLDOWN)
  - After bypassing cooldown via fake-time DB rewrite, the 2nd request is
    accepted (200) — proving the limit really is "1 per cooldown window"
  - 3rd request the same day is rejected (429 RESET_DAILY_LIMIT)
  - Unregistered email always returns ok=True silently (no leak / no limit hit)

Run from project root:
    python scripts/smoke_forgot_ratelimit.py
"""
from __future__ import annotations

import logging
import os
import secrets
import sys

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

# Lower the cooldown / daily-limit so the test is fast
os.environ["AUTH_RESET_COOLDOWN_SECONDS"] = "60"
os.environ["AUTH_RESET_DAILY_LIMIT"]      = "2"

from fastapi.testclient import TestClient
from app.api.server import app
from app.config.settings import settings
from app.storage.postgres.users_repo import get_users_repo


def _bypass_cooldown(email: str, seconds: int) -> None:
    """Rewrite created_at on this email's reset tokens so the next call thinks
    the cooldown has elapsed but the daily window has not."""
    repo = get_users_repo()
    with repo._connect() as conn:
        conn.execute(
            """
            UPDATE email_verification_tokens
            SET created_at = created_at - (%s * INTERVAL '1 second')
            WHERE LOWER(email) = LOWER(%s) AND purpose = 'reset_password'
            """,
            (seconds, email),
        )


def main() -> int:
    settings.auth_reset_cooldown_seconds = 60
    settings.auth_reset_daily_limit = 2
    print(f"[setup] cooldown={settings.auth_reset_cooldown_seconds}s  daily={settings.auth_reset_daily_limit}")

    with TestClient(app) as client:
        repo = get_users_repo()
        # Make sure no real emails go out — force the dev-fallback logger
        settings.smtp_host = ""

        email = f"fp_{secrets.token_hex(4)}@gmail.com"
        pwd = "fpPass!2026"
        r = client.post("/api/auth/email/register",
                        json={"email": email, "password": pwd, "display_name": "fp-user"})
        assert r.status_code == 200, r.text
        ident = repo.get_identity("email", email)
        repo.mark_identity_verified(ident.id)
        print(f"[setup] registered + verified {email}")

        # 1) First request → OK
        print("[1] first forgot")
        r = client.post("/api/auth/email/forgot", json={"email": email})
        assert r.status_code == 200, ("first should be 200", r.status_code, r.text)
        print("   [OK]", r.json())

        # 2) Immediately again → 429 RESET_COOLDOWN
        print("[2] second forgot inside cooldown")
        r = client.post("/api/auth/email/forgot", json={"email": email})
        assert r.status_code == 429, ("expected 429 cooldown", r.status_code, r.text)
        body = r.json()
        assert body["detail"]["code"] == "RESET_COOLDOWN", body
        print("   [OK]", body["detail"]["message"])

        # 3) Fake-time past the cooldown but still in 24h → OK
        print("[3] bypass cooldown via DB rewrite, try again")
        _bypass_cooldown(email, settings.auth_reset_cooldown_seconds + 5)
        r = client.post("/api/auth/email/forgot", json={"email": email})
        assert r.status_code == 200, ("expected 200 after cooldown", r.status_code, r.text)
        print("   [OK]", r.json())

        # 4) Bypass cooldown once more, now should be blocked by daily cap
        print("[4] bypass cooldown again, 3rd attempt blocked by daily cap")
        _bypass_cooldown(email, settings.auth_reset_cooldown_seconds + 5)
        r = client.post("/api/auth/email/forgot", json={"email": email})
        assert r.status_code == 429, ("expected 429 daily", r.status_code, r.text)
        body = r.json()
        assert body["detail"]["code"] == "RESET_DAILY_LIMIT", body
        print("   [OK]", body["detail"]["message"])

        # 5) Unregistered email → silently ok=True, no leak
        print("[5] unregistered email returns 200 silently")
        r = client.post("/api/auth/email/forgot",
                        json={"email": f"ghost_{secrets.token_hex(4)}@gmail.com"})
        assert r.status_code == 200, ("expected 200 for ghost", r.status_code, r.text)
        print("   [OK]", r.json())

    print("\nAll forgot-password rate-limit checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
