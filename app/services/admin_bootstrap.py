"""Startup-time admin bootstrap + legacy data migration.

On boot:
  1. Ensure the configured AUTH_ADMIN_EMAIL user exists, is verified, and
     has is_admin=True. New users are created with AUTH_ADMIN_INITIAL_PASSWORD
     if provided.
  2. Walk existing data/memory/sessions/*.json and tag any file without
     `owner_user_id` so the admin claims them.

Both steps are idempotent — safe to run on every startup.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from app.config.settings import settings
from app.services import auth_service
from app.storage.postgres.users_repo import User, get_users_repo

logger = logging.getLogger(__name__)


def ensure_admin_user() -> Optional[User]:
    """Return the admin User, creating / promoting as needed.

    Returns None if AUTH_ADMIN_EMAIL is not configured."""
    email = (settings.auth_admin_email or "").strip().lower()
    if not email:
        logger.info("Admin bootstrap skipped: AUTH_ADMIN_EMAIL not set")
        return None

    repo = get_users_repo()
    identity = repo.get_identity("email", email)

    if identity:
        # Already exists — only promote / verify if not already.
        user = repo.get_user(identity.user_id)
        if user is None:
            logger.warning("Admin email %s has identity but no user row — skipping", email)
            return None
        changed = False
        if not user.is_admin:
            repo.set_admin(user.id, True)
            changed = True
        if identity.verified_at is None:
            repo.mark_identity_verified(identity.id)
            changed = True
        if changed:
            logger.info("Admin bootstrap: promoted existing user %s to admin", email)
        else:
            logger.info("Admin bootstrap: %s already admin", email)
        return repo.get_user(user.id)

    # Account doesn't exist — create it if we have a password.
    pwd = settings.auth_admin_initial_password
    if not pwd:
        logger.warning(
            "Admin bootstrap: %s not registered yet and AUTH_ADMIN_INITIAL_PASSWORD "
            "is empty. Register that email manually, then restart to claim admin.",
            email,
        )
        return None

    display_name = settings.auth_admin_display_name or "Admin"
    user = repo.create_user(
        display_name=display_name,
        primary_email=email,
        is_admin=True,
    )
    repo.add_identity(
        user_id=user.id,
        provider="email",
        provider_uid=email,
        credential=auth_service.hash_password(pwd),
        verified=True,
    )
    logger.warning(
        "Admin bootstrap: created %s with the password from AUTH_ADMIN_INITIAL_PASSWORD. "
        "Log in once and change it.",
        email,
    )
    return user


# ── Session JSON migration ────────────────────────────────────────────────

_SESSIONS_DIR = os.path.join(".", "data", "memory", "sessions")


def claim_orphan_sessions(admin_user_id: str) -> dict:
    """Walk every session JSON; tag any file lacking owner_user_id."""
    if not os.path.isdir(_SESSIONS_DIR):
        return {"claimed": 0, "already_owned": 0, "skipped_malformed": 0}

    claimed = already = malformed = 0
    for fname in os.listdir(_SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(_SESSIONS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            malformed += 1
            logger.debug("Skipping malformed session %s: %s", fname, exc)
            continue

        if data.get("owner_user_id"):
            already += 1
            continue

        data["owner_user_id"] = admin_user_id
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            claimed += 1
        except Exception as exc:
            logger.warning("Failed to write owner_user_id on %s: %s", fname, exc)

    if claimed:
        logger.warning(
            "Session migration: claimed %d orphan session(s) for admin %s "
            "(%d already owned, %d malformed)",
            claimed, admin_user_id, already, malformed,
        )
    else:
        logger.info(
            "Session migration: 0 to claim (%d already owned, %d malformed)",
            already, malformed,
        )
    return {"claimed": claimed, "already_owned": already, "skipped_malformed": malformed}


def claim_legacy_files(admin_user_id: str) -> dict:
    """Register pre-existing upload files (sitting at the root of each upload
    directory, not yet under a per-user subdir) as admin-owned, and add their
    sizes to the admin's `user_storage` counter.

    Idempotent — files already tracked by storage_key are skipped."""
    from app.storage.postgres.files_repo import get_files_repo

    repo = get_files_repo()
    summary = {"claimed": 0, "already_tracked": 0, "skipped": 0}

    # category → directory + extensions allowed
    targets = {
        "upload": (os.path.join(".", "data", "uploads"),
                   {".pdf", ".pptx", ".txt", ".md", ".text", ".rst"}),
        "image":  (os.path.join(".", "data", "images", "uploads"),
                   {".png", ".jpg", ".jpeg", ".webp", ".bmp"}),
        "figure": (os.path.join(".", "data", "figure", "uploads"),
                   {".pdf", ".txt", ".md", ".text", ".rst"}),
    }

    for category, (root, allowed_exts) in targets.items():
        if not os.path.isdir(root):
            continue
        # Only files at the IMMEDIATE root of the dir (not inside per-user subdirs)
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in allowed_exts:
                summary["skipped"] += 1
                continue
            if repo.get_by_storage_key(path):
                summary["already_tracked"] += 1
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                summary["skipped"] += 1
                continue
            try:
                repo.insert_with_quota(
                    user_id=admin_user_id,
                    category=category,
                    original_name=name,
                    storage_key=path,
                    size_bytes=size,
                    mime_type=None,
                )
                summary["claimed"] += 1
            except Exception as exc:
                logger.debug("Skip legacy file %s: %s", path, exc)
                summary["skipped"] += 1

    if summary["claimed"]:
        logger.warning(
            "Legacy files claimed for admin %s: %s",
            admin_user_id, summary,
        )
    else:
        logger.info("No new legacy files to claim: %s", summary)
    return summary


def claim_legacy_libraries(admin_user_id: str) -> dict:
    """Stamp owner_user_id=admin on any library registry entry that has none.

    This covers the default `lt_docs` library plus any user-created libraries
    that pre-date Phase 4a."""
    from app.rag.long_term.store import get_lt_rag_store
    lt = get_lt_rag_store()
    reg = lt._load_registry()
    claimed = 0
    for lid, info in reg.items():
        if not info.get("owner_user_id"):
            info["owner_user_id"] = admin_user_id
            claimed += 1
    if claimed:
        lt._save_registry(reg)
        logger.warning("Library migration: claimed %d library/libraries for admin %s",
                       claimed, admin_user_id)
    else:
        logger.info("Library migration: 0 to claim")
    return {"claimed": claimed}


def claim_legacy_notes(admin_user_id: str) -> dict:
    """UPDATE notes SET user_id=<admin.id> WHERE user_id='local' (or any non-UUID legacy)."""
    from app.config.settings import settings
    if not settings.database_url:
        return {"claimed": 0}
    import psycopg
    with psycopg.connect(settings.database_url) as conn:
        # Only touch the legacy 'local' tag — leave real UUIDs untouched.
        cur = conn.execute(
            "UPDATE notes SET user_id = %s WHERE user_id = 'local'",
            (admin_user_id,),
        )
        n = cur.rowcount or 0
    if n:
        logger.warning("Notes migration: reassigned %d 'local' notes to admin %s", n, admin_user_id)
    else:
        logger.info("Notes migration: 0 'local' notes")
    return {"claimed": n}


def claim_legacy_day_tasks(admin_user_id: str) -> dict:
    """Same idea for day_tasks: 'local' → admin."""
    from app.config.settings import settings
    if not settings.database_url:
        return {"claimed": 0}
    import psycopg
    with psycopg.connect(settings.database_url) as conn:
        cur = conn.execute(
            "UPDATE day_tasks SET user_id = %s WHERE user_id = 'local'",
            (admin_user_id,),
        )
        n = cur.rowcount or 0
    if n:
        logger.warning("Day tasks migration: reassigned %d 'local' tasks to admin %s",
                       n, admin_user_id)
    else:
        logger.info("Day tasks migration: 0 'local' tasks")
    return {"claimed": n}


_READING_DIR = os.path.join(".", "data", "reading")


def claim_legacy_reading(admin_user_id: str) -> dict:
    """Stamp user_id=admin on pre-existing annotations / progress entries."""
    ann_path = os.path.join(_READING_DIR, "annotations.json")
    prog_path = os.path.join(_READING_DIR, "progress.json")
    summary = {"annotations_claimed": 0, "progress_remapped": 0}

    # Annotations — list of dicts. Add user_id if missing.
    if os.path.exists(ann_path):
        try:
            with open(ann_path, encoding="utf-8") as f:
                anns = json.load(f) or []
            changed = False
            for a in anns:
                if isinstance(a, dict) and not a.get("user_id"):
                    a["user_id"] = admin_user_id
                    changed = True
                    summary["annotations_claimed"] += 1
            if changed:
                with open(ann_path, "w", encoding="utf-8") as f:
                    json.dump(anns, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.debug("Annotation migration skipped: %s", exc)

    # Progress — was {doc_id: {…}}; now {user_id::doc_id: {…}}. Migrate old shape.
    if os.path.exists(prog_path):
        try:
            with open(prog_path, encoding="utf-8") as f:
                prog = json.load(f) or {}
            new_prog = {}
            changed = False
            for key, value in prog.items():
                if "::" in key:
                    new_prog[key] = value          # already in new shape
                else:
                    new_key = f"{admin_user_id}::{key}"
                    if isinstance(value, dict):
                        value = dict(value)
                        value.setdefault("user_id", admin_user_id)
                        value.setdefault("doc_id", key)
                    new_prog[new_key] = value
                    changed = True
                    summary["progress_remapped"] += 1
            if changed:
                with open(prog_path, "w", encoding="utf-8") as f:
                    json.dump(new_prog, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.debug("Reading progress migration skipped: %s", exc)

    if summary["annotations_claimed"] or summary["progress_remapped"]:
        logger.warning("Reading migration for admin %s: %s", admin_user_id, summary)
    else:
        logger.info("Reading migration: nothing to claim")
    return summary


def claim_legacy_profile(admin_user_id: str) -> dict:
    """Promote the pre-Phase-4d single `user_profile` field to `user_profiles[admin]`."""
    try:
        from app.memory.long_term.store import LongTermMemoryStore
    except Exception:
        return {"claimed": 0}
    # Touching the singleton lazily — avoid coupling to memory.manager imports.
    store = LongTermMemoryStore()
    legacy = store._data.get("user_profile")  # pre-Phase 4d global profile
    if not legacy or not isinstance(legacy, dict):
        return {"claimed": 0}
    per_user = store._data.setdefault("user_profiles", {})
    if admin_user_id in per_user:
        return {"claimed": 0}     # admin already has their own profile
    per_user[admin_user_id] = legacy
    # Keep the legacy field as-is so unauthenticated paths still resolve a profile.
    store.save()
    logger.warning("Profile migration: copied legacy user_profile to admin %s", admin_user_id)
    return {"claimed": 1}


def run_startup_bootstrap() -> None:
    """Single entry point called from FastAPI lifespan."""
    try:
        admin = ensure_admin_user()
    except Exception as exc:
        logger.warning("Admin bootstrap failed: %s", exc)
        return
    if admin is None:
        return
    for label, fn in [
        ("sessions",   lambda: claim_orphan_sessions(admin.id)),
        ("files",      lambda: claim_legacy_files(admin.id)),
        ("libraries",  lambda: claim_legacy_libraries(admin.id)),
        ("notes",      lambda: claim_legacy_notes(admin.id)),
        ("day_tasks",  lambda: claim_legacy_day_tasks(admin.id)),
        ("reading",    lambda: claim_legacy_reading(admin.id)),
        ("profile",    lambda: claim_legacy_profile(admin.id)),
    ]:
        try:
            fn()
        except Exception as exc:
            logger.warning("%s migration failed: %s", label, exc)
