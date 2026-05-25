"""Postgres repository for user files + storage-quota tracking.

Both tables are created by `UsersRepository.init_schema()` (kept there so a
single connection-init runs the whole schema). This module only contains
CRUD + atomic quota mutations.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FileRecord:
    id: str
    user_id: str
    category: str
    original_name: str
    storage_key: str
    size_bytes: int
    mime_type: Optional[str]
    created_at: Optional[datetime]


class FilesRepository:
    """Sync Postgres repo, mirrors the pattern in users_repo / note_repository."""

    def __init__(self, database_url: str) -> None:
        import psycopg
        self._database_url = database_url
        self._psycopg = psycopg

    def _connect(self):
        return self._psycopg.connect(self._database_url)

    # ── files ─────────────────────────────────────────────────────────────

    def insert_with_quota(
        self,
        *,
        user_id: str,
        category: str,
        original_name: str,
        storage_key: str,
        size_bytes: int,
        mime_type: Optional[str],
    ) -> FileRecord:
        """Insert the file row AND bump user_storage.used_bytes in one transaction."""
        fid = str(uuid.uuid4())
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO files (id, user_id, category, original_name, storage_key, size_bytes, mime_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, user_id, category, original_name, storage_key, size_bytes, mime_type, created_at
                    """,
                    (fid, user_id, category, original_name, storage_key, size_bytes, mime_type),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO user_storage (user_id, used_bytes)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE
                      SET used_bytes = user_storage.used_bytes + EXCLUDED.used_bytes,
                          updated_at = NOW()
                    """,
                    (user_id, size_bytes),
                )
        return self._row_to_record(row)

    def list_by_user(self, user_id: str, category: Optional[str] = None) -> list[FileRecord]:
        sql = """
            SELECT id, user_id, category, original_name, storage_key, size_bytes, mime_type, created_at
            FROM files WHERE user_id = %s
        """
        params: tuple = (user_id,)
        if category:
            sql += " AND category = %s"
            params = (user_id, category)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get(self, file_id: str) -> Optional[FileRecord]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, category, original_name, storage_key, size_bytes, mime_type, created_at
                FROM files WHERE id = %s
                """,
                (file_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_storage_key(self, storage_key: str) -> Optional[FileRecord]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, category, original_name, storage_key, size_bytes, mime_type, created_at
                FROM files WHERE storage_key = %s
                """,
                (storage_key,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def delete_with_quota(self, *, file_id: str, user_id: str) -> Optional[FileRecord]:
        """Delete the file row and decrement quota atomically.

        Returns the deleted record on success, None if it didn't exist or didn't
        belong to *user_id*."""
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    DELETE FROM files WHERE id = %s AND user_id = %s
                    RETURNING id, user_id, category, original_name, storage_key, size_bytes, mime_type, created_at
                    """,
                    (file_id, user_id),
                ).fetchone()
                if not row:
                    return None
                size = row[5]
                conn.execute(
                    """
                    UPDATE user_storage
                    SET used_bytes = GREATEST(used_bytes - %s, 0),
                        updated_at = NOW()
                    WHERE user_id = %s
                    """,
                    (size, user_id),
                )
        return self._row_to_record(row)

    # ── quota ─────────────────────────────────────────────────────────────

    def get_used_bytes(self, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT used_bytes FROM user_storage WHERE user_id = %s",
                (user_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def add_bytes(self, user_id: str, delta_bytes: int) -> None:
        """Used by the legacy-file backfill that only writes the user_storage counter."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_storage (user_id, used_bytes)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                  SET used_bytes = user_storage.used_bytes + EXCLUDED.used_bytes,
                      updated_at = NOW()
                """,
                (user_id, delta_bytes),
            )

    # ── internals ─────────────────────────────────────────────────────────

    def _row_to_record(self, row) -> FileRecord:
        return FileRecord(
            id=str(row[0]),
            user_id=str(row[1]),
            category=row[2],
            original_name=row[3],
            storage_key=row[4],
            size_bytes=int(row[5]),
            mime_type=row[6],
            created_at=row[7],
        )


# ── singleton ─────────────────────────────────────────────────────────────

_repo: Optional[FilesRepository] = None
_repo_lock = threading.Lock()


def get_files_repo() -> FilesRepository:
    global _repo
    if _repo is not None:
        return _repo
    with _repo_lock:
        if _repo is not None:
            return _repo
        from app.config.settings import settings
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set — files repo requires PostgreSQL.")
        _repo = FilesRepository(settings.database_url)
        return _repo
