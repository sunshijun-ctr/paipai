"""Send a single test email using the configured SMTP_* settings.

Usage:
    python scripts/smoke_email.py recipient@example.com

If SMTP_HOST is empty in .env, this falls back to printing the email body
to the console — same behaviour as the auth flows.
"""
from __future__ import annotations

import asyncio
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from app.config.settings import settings
from app.services import email_service


async def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/smoke_email.py recipient@example.com")
        return 2
    to = sys.argv[1].strip()
    print(f"SMTP_HOST       = {settings.smtp_host or '(empty — will print to console)'}")
    print(f"SMTP_PORT       = {settings.smtp_port}")
    print(f"SMTP_USER       = {settings.smtp_user}")
    print(f"SMTP_FROM       = {settings.smtp_from_name} <{settings.smtp_from_address}>")
    print(f"Sending to      → {to}")
    print()

    subject, html, text = email_service.render_activation_email(
        display_name="测试用户",
        link=f"{settings.auth_public_base_url.rstrip('/')}/api/auth/email/verify?token=DEMO_TOKEN",
    )
    try:
        await email_service.send_email(to=to, subject=subject, html=html, text=text)
    except Exception as exc:
        print(f"\n✗ FAILED: {type(exc).__name__}: {exc}")
        return 1
    print("\n✓ Email submitted to SMTP server (or printed in dev mode).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
