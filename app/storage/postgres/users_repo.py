"""Postgres repository for the auth subsystem.

Tables:
  users                       — canonical user record
  user_identities             — login methods (email / phone / qq) bound to a user
  refresh_tokens              — long-lived refresh tokens (sha256-hashed)
  email_verification_tokens   — register activation + password reset tokens

Everything sync-psycopg, mirroring the existing note_repository.py pattern.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Public dataclasses ─────────────────────────────────────────────────────

@dataclass
class User:
    id: str
    display_name: str
    avatar_url: Optional[str] = None
    primary_email: Optional[str] = None
    primary_phone: Optional[str] = None
    status: str = "active"
    is_admin: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Identity:
    id: str
    user_id: str
    provider: str          # email / phone / qq
    provider_uid: str
    credential: Optional[str] = None
    verified_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
    created_at: Optional[datetime] = None


# ── Repository ─────────────────────────────────────────────────────────────

class UsersRepository:
    """Sync Postgres repo. One instance per process; init_schema is idempotent."""

    def __init__(self, database_url: str) -> None:
        import psycopg
        self._database_url = database_url
        self._psycopg = psycopg
        self.init_schema()

    def _connect(self):
        return self._psycopg.connect(self._database_url)

    # -- Schema ------------------------------------------------------------

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id              UUID PRIMARY KEY,
                    display_name    TEXT NOT NULL,
                    avatar_url      TEXT,
                    primary_email   TEXT,
                    primary_phone   TEXT,
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            # Idempotent migration: add is_admin if missing
            conn.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_identities (
                    id              UUID PRIMARY KEY,
                    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider        TEXT NOT NULL,
                    provider_uid    TEXT NOT NULL,
                    credential      TEXT,
                    verified_at     TIMESTAMPTZ,
                    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (provider, provider_uid)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    id              UUID PRIMARY KEY,
                    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash      TEXT NOT NULL UNIQUE,
                    expires_at      TIMESTAMPTZ NOT NULL,
                    revoked_at      TIMESTAMPTZ,
                    user_agent      TEXT,
                    ip              TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id) WHERE revoked_at IS NULL"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verification_tokens (
                    token_hash      TEXT PRIMARY KEY,
                    email           TEXT NOT NULL,
                    user_id         UUID,
                    purpose         TEXT NOT NULL,
                    expires_at      TIMESTAMPTZ NOT NULL,
                    used_at         TIMESTAMPTZ,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            # ── Phase 2: per-user file storage + quota ──────────────────────
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id              UUID PRIMARY KEY,
                    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    category        TEXT NOT NULL,         -- 'upload' | 'image' | 'figure'
                    original_name   TEXT NOT NULL,
                    storage_key     TEXT NOT NULL UNIQUE,  -- absolute or repo-relative path
                    size_bytes      BIGINT NOT NULL,
                    mime_type       TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_category ON files(user_id, category)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_storage (
                    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    used_bytes      BIGINT NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    # -- users --------------------------------------------------------------

    def create_user(self, *, display_name: str, primary_email: Optional[str] = None,
                    primary_phone: Optional[str] = None, avatar_url: Optional[str] = None,
                    is_admin: bool = False) -> User:
        uid = str(uuid.uuid4())
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO users (id, display_name, primary_email, primary_phone, avatar_url, is_admin)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, display_name, avatar_url, primary_email, primary_phone, status, is_admin, created_at, updated_at
                """,
                (uid, display_name, primary_email, primary_phone, avatar_url, is_admin),
            ).fetchone()
        return self._row_to_user(row)

    def get_user(self, user_id: str) -> Optional[User]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, display_name, avatar_url, primary_email, primary_phone, status, is_admin, created_at, updated_at
                FROM users WHERE id = %s
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[User]:
        """Look up the user who actually has an email identity for *email*.

        Prefers users with a real `user_identities` row over orphan profile rows
        (e.g. left over from failed registrations). Falls back to a primary_email
        match if no identity exists."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.display_name, u.avatar_url, u.primary_email, u.primary_phone,
                       u.status, u.is_admin, u.created_at, u.updated_at
                FROM users u
                JOIN user_identities i
                  ON i.user_id = u.id AND i.provider = 'email'
                 AND LOWER(i.provider_uid) = LOWER(%s)
                ORDER BY u.created_at DESC
                LIMIT 1
                """,
                (email,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT id, display_name, avatar_url, primary_email, primary_phone,
                           status, is_admin, created_at, updated_at
                    FROM users WHERE LOWER(primary_email) = LOWER(%s)
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (email,),
                ).fetchone()
        return self._row_to_user(row) if row else None

    def update_primary_email(self, user_id: str, email: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET primary_email = %s, updated_at = NOW() WHERE id = %s",
                (email, user_id),
            )

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_admin = %s, updated_at = NOW() WHERE id = %s",
                (is_admin, user_id),
            )

    # -- identities --------------------------------------------------------

    def get_identity(self, provider: str, provider_uid: str) -> Optional[Identity]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, provider, provider_uid, credential, verified_at, metadata, created_at
                FROM user_identities WHERE provider = %s AND provider_uid = %s
                """,
                (provider, provider_uid),
            ).fetchone()
        return self._row_to_identity(row) if row else None

    def list_identities(self, user_id: str) -> list[Identity]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, provider, provider_uid, credential, verified_at, metadata, created_at
                FROM user_identities WHERE user_id = %s ORDER BY created_at ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def add_identity(self, *, user_id: str, provider: str, provider_uid: str,
                     credential: Optional[str] = None, verified: bool = False,
                     metadata: Optional[dict] = None) -> Identity:
        ident_id = str(uuid.uuid4())
        verified_at = datetime.now(timezone.utc) if verified else None
        meta = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO user_identities (id, user_id, provider, provider_uid, credential, verified_at, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, user_id, provider, provider_uid, credential, verified_at, metadata, created_at
                """,
                (ident_id, user_id, provider, provider_uid, credential, verified_at, meta),
            ).fetchone()
        return self._row_to_identity(row)

    def mark_identity_verified(self, identity_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE user_identities SET verified_at = NOW() WHERE id = %s",
                (identity_id,),
            )

    def update_identity_credential(self, identity_id: str, credential: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE user_identities SET credential = %s WHERE id = %s",
                (credential, identity_id),
            )

    def delete_identity(self, identity_id: str, user_id: str) -> bool:
        """Refuse to delete the last identity of a user."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM user_identities WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            if row and row[0] <= 1:
                return False
            cur = conn.execute(
                "DELETE FROM user_identities WHERE id = %s AND user_id = %s",
                (identity_id, user_id),
            )
            return cur.rowcount > 0

    # -- refresh tokens ----------------------------------------------------

    def store_refresh_token(self, *, user_id: str, token: str, ttl_days: int,
                            user_agent: Optional[str] = None,
                            ip: Optional[str] = None) -> str:
        rec_id = str(uuid.uuid4())
        token_hash = _sha256(token)
        expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, user_agent, ip)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (rec_id, user_id, token_hash, expires, user_agent, ip),
            )
        return rec_id

    def consume_refresh_token(self, token: str) -> Optional[str]:
        """Return user_id if token is valid + not revoked + not expired; else None.
        Does NOT mutate — call revoke_refresh_token() to rotate."""
        token_hash = _sha256(token)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id FROM refresh_tokens
                WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > NOW()
                """,
                (token_hash,),
            ).fetchone()
        return str(row[0]) if row else None

    def revoke_refresh_token(self, token: str) -> None:
        token_hash = _sha256(token)
        with self._connect() as conn:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL",
                (token_hash,),
            )

    def revoke_all_for_user(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )

    # -- email verification tokens ----------------------------------------

    def issue_email_token(self, *, email: str, purpose: str, user_id: Optional[str],
                          ttl_minutes: int = 60) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = _sha256(token)
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO email_verification_tokens (token_hash, email, user_id, purpose, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (token_hash, email, user_id, purpose, expires),
            )
        return token

    def count_email_tokens_since(self, *, email: str, purpose: str,
                                 since: datetime) -> int:
        """How many tokens were issued for (email, purpose) at-or-after *since*?

        Used by the forgot-password rate limiter — counts ALL issuances
        (used or unused), since each issuance triggers a real email send."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM email_verification_tokens
                WHERE LOWER(email) = LOWER(%s)
                  AND purpose = %s
                  AND created_at >= %s
                """,
                (email, purpose, since),
            ).fetchone()
        return int(row[0]) if row else 0

    def consume_email_token(self, token: str, purpose: str) -> Optional[dict]:
        """Mark token as used and return its row, or None if invalid/expired/used."""
        token_hash = _sha256(token)
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE email_verification_tokens
                SET used_at = NOW()
                WHERE token_hash = %s AND purpose = %s AND used_at IS NULL AND expires_at > NOW()
                RETURNING email, user_id
                """,
                (token_hash, purpose),
            ).fetchone()
        if not row:
            return None
        return {"email": row[0], "user_id": str(row[1]) if row[1] else None}

    # -- helpers -----------------------------------------------------------

    def _row_to_user(self, row) -> User:
        return User(
            id=str(row[0]),
            display_name=row[1],
            avatar_url=row[2],
            primary_email=row[3],
            primary_phone=row[4],
            status=row[5],
            is_admin=bool(row[6]),
            created_at=row[7],
            updated_at=row[8],
        )

    def _row_to_identity(self, row) -> Identity:
        meta = row[6] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return Identity(
            id=str(row[0]),
            user_id=str(row[1]),
            provider=row[2],
            provider_uid=row[3],
            credential=row[4],
            verified_at=row[5],
            metadata=meta,
            created_at=row[7],
        )


# ── Singleton accessor ────────────────────────────────────────────────────

_repo: Optional[UsersRepository] = None
_repo_lock = threading.Lock()


def get_users_repo() -> UsersRepository:
    """Return the process-wide UsersRepository, initialising on first call.

    Raises RuntimeError if DATABASE_URL is not configured."""
    global _repo
    if _repo is not None:
        return _repo
    with _repo_lock:
        if _repo is not None:
            return _repo
        from app.config.settings import settings
        if not settings.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set — auth subsystem requires PostgreSQL. "
                "Set DATABASE_URL in .env."
            )
        _repo = UsersRepository(settings.database_url)
        logger.info("UsersRepository initialised against %s", _safe_db_log(settings.database_url))
        return _repo


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_db_log(url: str) -> str:
    """Strip password from DSN for safe logging."""
    if "://" not in url or "@" not in url:
        return url
    head, tail = url.split("://", 1)
    if "@" not in tail:
        return url
    creds, host = tail.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{head}://{user}:***@{host}"
    return url
