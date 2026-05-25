"""Per-user file storage with quota enforcement.

Public API:
  save_upload(user, content, original_name, category)   → FileRecord
  delete_file_by_id(user, file_id)                      → FileRecord | None
  user_quota(user)                                      → {used, limit, free, percent}

Quota policy:
  - Each non-admin user has settings.auth_storage_limit_bytes total.
  - A single upload is additionally capped by settings.auth_max_upload_bytes.
  - Admins are exempt from both checks (still tracked in user_storage).
  - Counter updates happen in the same DB transaction as the file row,
    so partial failures cannot leak quota.

On-disk layout (each category keeps its own root for path-validation reuse):
  ./data/uploads/{user_id}/{uuid}_{original_name}
  ./data/images/uploads/{user_id}/{uuid}_{original_name}
  ./data/figure/uploads/{user_id}/{uuid}_{original_name}
"""
from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

from app.config.settings import settings
from app.storage.postgres.files_repo import FileRecord, get_files_repo
from app.storage.postgres.users_repo import User

logger = logging.getLogger(__name__)

# Root directories per category — must match the constants in app/api/server.py
# so that the existing path-traversal validators continue to allow files
# stored at ./data/<root>/{user_id}/...
_CATEGORY_ROOTS = {
    "upload": os.path.join(".", "data", "uploads"),
    "image":  os.path.join(".", "data", "images", "uploads"),
    "figure": os.path.join(".", "data", "figure", "uploads"),
}


@dataclass
class QuotaInfo:
    used_bytes: int
    limit_bytes: int
    free_bytes: int
    percent: float
    is_admin: bool


def _user_dir(category: str, user_id: str) -> str:
    root = _CATEGORY_ROOTS.get(category)
    if root is None:
        raise ValueError(f"unknown storage category: {category}")
    path = os.path.join(root, user_id)
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_quota(user: User, incoming_bytes: int) -> None:
    if incoming_bytes <= 0:
        return
    if incoming_bytes > settings.auth_max_upload_bytes:
        max_mb = settings.auth_max_upload_bytes / 1024 / 1024
        raise HTTPException(
            413,
            f"单个文件超过上限 {max_mb:.0f}MB（当前 {incoming_bytes / 1024 / 1024:.1f}MB）",
        )
    if user.is_admin:
        return  # admin is exempt from total-quota check
    used = get_files_repo().get_used_bytes(user.id)
    limit = settings.auth_storage_limit_bytes
    if used + incoming_bytes > limit:
        used_mb = used / 1024 / 1024
        new_mb = incoming_bytes / 1024 / 1024
        limit_mb = limit / 1024 / 1024
        raise HTTPException(
            413,
            f"存储空间不足：已用 {used_mb:.1f}MB / {limit_mb:.0f}MB，本次上传 {new_mb:.1f}MB 会超额",
        )


def save_upload(
    *,
    user: User,
    content: bytes,
    original_name: str,
    category: str,
    mime_type: Optional[str] = None,
) -> tuple[FileRecord, str]:
    """Persist *content* under the user's dir, record it, return (record, abs_path).

    Quota and per-file size limits are enforced before any disk write."""
    if category not in _CATEGORY_ROOTS:
        raise ValueError(f"unknown storage category: {category}")

    size = len(content)
    _ensure_quota(user, size)

    safe_original = os.path.basename(original_name or "file")
    ext = os.path.splitext(safe_original)[1].lower()
    safe_name = f"{uuid.uuid4().hex[:8]}_{safe_original}"
    dest_dir = _user_dir(category, user.id)
    dest_path = os.path.join(dest_dir, safe_name)

    # Write to disk first; if the DB insert fails afterwards we clean up.
    with open(dest_path, "wb") as fh:
        fh.write(content)

    try:
        repo = get_files_repo()
        record = repo.insert_with_quota(
            user_id=user.id,
            category=category,
            original_name=safe_original,
            storage_key=dest_path,
            size_bytes=size,
            mime_type=mime_type or mimetypes.guess_type(safe_original)[0],
        )
    except Exception:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise

    logger.info(
        "Stored upload (%s) for user=%s size=%d → %s",
        category, user.id, size, dest_path,
    )
    return record, dest_path


def delete_file_by_id(*, user: User, file_id: str) -> Optional[FileRecord]:
    """Delete the file from disk and DB. Admin can delete any file."""
    repo = get_files_repo()
    rec = repo.get(file_id)
    if rec is None:
        return None
    if not user.is_admin and rec.user_id != user.id:
        return None

    deleted = repo.delete_with_quota(file_id=file_id, user_id=rec.user_id)
    if deleted:
        try:
            os.remove(deleted.storage_key)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to remove file from disk: %s (%s)", deleted.storage_key, exc)
    return deleted


def user_quota(user: User) -> QuotaInfo:
    used = get_files_repo().get_used_bytes(user.id)
    limit = settings.auth_storage_limit_bytes
    free = max(limit - used, 0)
    percent = round((used / limit) * 100, 1) if limit > 0 else 0.0
    return QuotaInfo(
        used_bytes=used,
        limit_bytes=limit,
        free_bytes=free,
        percent=percent,
        is_admin=user.is_admin,
    )
