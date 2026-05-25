"""FastAPI server — Research Assistant web interface.

Endpoints:
  GET    /                        serve the chat page
  GET    /api/sessions            list all sessions (newest first)
  POST   /api/sessions            create a new session
  GET    /api/sessions/{sid}      session state + full conversation history
  DELETE /api/sessions/{sid}      delete a session
  POST   /api/upload              upload a document into a session
  WS     /ws                      persistent WebSocket for chat
"""
import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth_router import router as auth_router
from app.api.citation_router import router as citation_router
from app.api.deps import optional_user, require_admin, require_user, require_user_ws
from app.api.qq_webhook_api import router as qq_webhook_router
from app.api.qq_webhook_api import set_qq_event_receiver
from app.storage.postgres.users_repo import User
from app.channels.qq.qq_client import QQClient
from app.channels.qq.qq_config import QQConfig
from app.channels.qq.qq_event_receiver import QQEventReceiver
from app.channels.qq.qq_sender import QQSender
from app.memory.manager import MemoryManager
from app.orchestrator.orchestrator import Orchestrator
from app.orchestrator.state_manager import sync_memory_from_task_state
from app.services.agent_service import AgentService, format_paper_search_reply
from app.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

_SESSIONS_DIR = os.path.join(".", "data", "memory", "sessions")
_UPLOAD_DIR   = os.path.join(".", "data", "uploads")
_IMAGE_UPLOAD_DIR = os.path.join(".", "data", "images", "uploads")
_FIGURE_UPLOAD_DIR = os.path.join(".", "data", "figure", "uploads")
_FIGURE_OUTPUT_DIR = os.path.join(".", "data", "figure", "outputs")
_READING_DIR = os.path.join(".", "data", "reading")
_ANNOTATIONS_FILE = os.path.join(_READING_DIR, "annotations.json")
_READING_PROGRESS_FILE = os.path.join(_READING_DIR, "progress.json")
_ALLOWED_EXTS = {".pdf", ".pptx", ".txt", ".md", ".text", ".rst"}
_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_READABLE_ROOTS = [
    os.path.abspath(os.path.join(".", "data", "uploads")),
    os.path.abspath(os.path.join(".", "data", "papers")),
    os.path.abspath(os.path.join(".", "data", "images", "uploads")),
]

_orch: Optional[Orchestrator] = None
_running_tasks: dict[str, asyncio.Task] = {}
_sessions: dict[str, MemoryManager] = {}   # sid → MemoryManager (lazy-loaded)
_figure_docs: dict[str, dict] = {}

_MONITOR_INIT_SQL = """
CREATE TABLE IF NOT EXISTS llm_token_usage (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent_name TEXT NOT NULL DEFAULT 'default',
    task_name TEXT,
    session_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    is_streaming BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'success',
    error_msg TEXT,
    cost_yuan NUMERIC(12, 6)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_token_usage (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_provider ON llm_token_usage (provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_token_usage (model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_token_usage (agent_name, created_at DESC);
"""


def _read_json_file(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as exc:
        logger.warning("Failed to read json file %s: %s", path, exc)
        return default


def _write_json_file(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _reading_annotations() -> list[dict]:
    data = _read_json_file(_ANNOTATIONS_FILE, [])
    return data if isinstance(data, list) else []


def _reading_progress() -> dict:
    data = _read_json_file(_READING_PROGRESS_FILE, {})
    return data if isinstance(data, dict) else {}


def _get_mem(sid: str) -> MemoryManager:
    """Return a MemoryManager for *sid*, loading from disk if not yet in cache."""
    if sid not in _sessions:
        mem = MemoryManager(session_id=sid, llm=_orch.llm)
        # Drop stored-paper entries whose PDFs no longer exist on disk
        valid = [p for p in mem.short_term.stored_papers
                 if os.path.exists(p.get("local_path", ""))]
        mem.short_term.stored_papers = valid
        _sessions[sid] = mem
    mem = _sessions[sid]
    if _orch:
        mem.set_llm(_orch.llm)
    return mem


def _session_owner_id(sid: str) -> str:
    """Return the owner_user_id stored in the session's JSON, or '' if missing/malformed."""
    cached = _sessions.get(sid)
    if cached is not None:
        return cached.short_term.owner_user_id or ""
    path = os.path.join(_SESSIONS_DIR, f"{sid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("owner_user_id") or "")
    except Exception:
        return ""


def _user_owns_session(user: "User", sid: str) -> bool:
    """Admin sees everything; others must own the session."""
    if user.is_admin:
        return True
    owner = _session_owner_id(sid)
    return bool(owner) and owner == user.id


def _ensure_session_owner(user: "User", sid: str) -> None:
    """Raise 404 if the current user is not allowed to touch this session.

    We deliberately return 404 (not 403) so we don't leak which session IDs exist."""
    if not _user_owns_session(user, sid):
        raise HTTPException(404, "session not found")


def _stamp_session_owner(mem: MemoryManager, user: "User") -> None:
    """If a session has no owner yet, claim it for *user* and persist."""
    if not mem.short_term.owner_user_id:
        mem.short_term.owner_user_id = user.id
        mem.save()


def _ensure_library_owner(user: "User", lib_id: str) -> None:
    """Raise 404 if *user* may not access this library.

    Admins bypass; owners of the library pass; everyone else gets 404 so we
    don't leak which lib_ids exist."""
    if user.is_admin:
        return
    from app.rag.long_term.store import get_lt_rag_store
    owner = get_lt_rag_store().get_library_owner(lib_id)
    if not owner or owner != user.id:
        raise HTTPException(404, "library not found")


def _paper_extra_meta(paper: dict) -> dict:
    keys = ("venue", "journal", "doi", "paper_id", "published_date", "authors", "citations", "source")
    meta = {key: paper.get(key) for key in keys if paper.get(key) not in (None, "")}
    if meta.get("source"):
        meta["paper_source"] = meta.pop("source")
    return meta


def _resolve_found_paper(found_papers: list[dict], body: dict) -> dict | None:
    if body.get("index") is not None:
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            raise HTTPException(400, "index must be an integer")
        # Frontend search result indices are 1-based.
        if 1 <= idx <= len(found_papers):
            return found_papers[idx - 1]
        if 0 <= idx < len(found_papers):
            return found_papers[idx]
        return None
    title = str(body.get("title") or "").strip().lower()
    if title:
        return next((p for p in found_papers if str(p.get("title") or "").strip().lower() == title), None)
    paper_id = str(body.get("paper_id") or body.get("paperId") or "").strip().lower()
    if paper_id:
        return next((p for p in found_papers if _paper_identity(p) == paper_id), None)
    return None


def _stored_paper_index(stored_papers: list[dict], paper: dict) -> int:
    paper_id = _paper_identity(paper)
    title = str(paper.get("title") or "").strip().lower()
    for idx, item in enumerate(stored_papers or []):
        if paper_id and _paper_identity(item) == paper_id:
            return idx
        if title and str(item.get("title") or "").strip().lower() == title:
            return idx
    return -1


def _paper_identity(paper: dict) -> str:
    return str(paper.get("paper_id") or paper.get("paperId") or "").strip().lower()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orch
    _orch = Orchestrator()
    # Admin bootstrap + legacy session ownership migration. Idempotent.
    try:
        from app.services.admin_bootstrap import run_startup_bootstrap
        run_startup_bootstrap()
    except Exception as exc:
        logger.warning("Admin bootstrap pipeline failed: %s", exc)
    # ResearchAgent's Postgres checkpointer (Phase C). Idempotent setup;
    # graph still runs without it if DB is missing.
    try:
        from app.orchestrator.research_checkpointer import init_research_checkpointer
        await init_research_checkpointer()
    except Exception as exc:
        logger.warning("Research checkpointer init failed: %s", exc)
    qq_config = QQConfig()
    qq_sender = None
    if qq_config.bot_app_id and qq_config.bot_secret:
        qq_sender = QQSender(QQClient(qq_config), max_reply_length=qq_config.max_reply_length)
    else:
        logger.warning("QQ channel started without sender credentials; webhook runs in dry-run mode")
    set_qq_event_receiver(QQEventReceiver(
        agent_service=AgentService(_orch, timeout_seconds=qq_config.reply_timeout_seconds),
        sender=qq_sender,
        config=qq_config,
    ))
    logger.info("Research Assistant web server ready")
    yield
    set_qq_event_receiver(None)
    try:
        from app.orchestrator.research_checkpointer import close_research_checkpointer
        await close_research_checkpointer()
    except Exception as exc:
        logger.warning("Research checkpointer close failed: %s", exc)
    logger.info("Server shutdown")


app = FastAPI(title="Research Assistant", lifespan=lifespan)
app.include_router(citation_router)
app.include_router(qq_webhook_router)
app.include_router(auth_router)
_static_dir = os.path.join(os.path.dirname(__file__), "static")


def _build_version() -> str:
    """Short identifier the frontend appends as `?v=<version>` to every
    static asset URL. Refreshes when any file under static/ changes (or
    on git HEAD movement) so users never serve a stale JS/CSS while
    holding a new HTML.

    Two strategies, in order of preference:
      1. git HEAD SHA — stable across restarts, perfect for production deploys
      2. md5 of all static file mtimes — works for dev where there's no git
    """
    import hashlib
    import pathlib
    try:
        head = pathlib.Path(".git/HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = pathlib.Path(".git") / head.split(" ", 1)[1].strip()
            return ref.read_text(encoding="utf-8").strip()[:8]
        return head[:8]
    except Exception:
        h = hashlib.md5()
        try:
            for p in sorted(pathlib.Path(_static_dir).rglob("*")):
                if p.is_file():
                    h.update(f"{p.name}:{p.stat().st_mtime_ns}".encode())
        except Exception:
            pass
        return h.hexdigest()[:8] or "dev"


_STATIC_VERSION = _build_version()
logger.info("Static asset version: %s", _STATIC_VERSION)


def _vite_entry(entry: str = "src/main.js") -> str:
    """Resolve a Vite manifest entry to its hashed output path.

    Reads ``app/api/static/dist/.vite/manifest.json`` (produced by
    ``cd web && npm run build``). When the file is missing — e.g. the
    bundle hasn't been built yet — we fall back to ``""`` and the
    index.html template loads no bundle. The legacy inline scripts keep
    working in the meantime.
    """
    import json
    import pathlib
    manifest_path = pathlib.Path(_static_dir) / "dist" / ".vite" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return "/static/dist/" + manifest[entry]["file"]
    except FileNotFoundError:
        logger.warning("Vite manifest not found at %s — run `cd web && npm run build`", manifest_path)
        return ""
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning("Vite manifest unreadable: %s", exc)
        return ""


logger.info("Frontend bundle entry (startup): %s", _vite_entry() or "(not built)")

# Serve /static/* directly from app/api/static/. Used by Phase 2.2 CSS
# extraction (and Phase 2.3 JS modules later). In production, Caddy
# overrides Cache-Control on this prefix to long-cache; in dev the
# default no-cache from FastAPI is fine.
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── Static auth pages ─────────────────────────────────────────────────────────

def _serve_static(filename: str, media_type: Optional[str] = None) -> FileResponse:
    path = os.path.join(_static_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(404, f"{filename} not found")
    return FileResponse(path, media_type=media_type)


@app.get("/login", response_class=HTMLResponse)
@app.get("/login.html", response_class=HTMLResponse)
async def login_page():
    return _serve_static("login.html", media_type="text/html")


@app.get("/register", response_class=HTMLResponse)
@app.get("/register.html", response_class=HTMLResponse)
async def register_page():
    return _serve_static("register.html", media_type="text/html")


@app.get("/account", response_class=HTMLResponse)
@app.get("/account.html", response_class=HTMLResponse)
async def account_page():
    return _serve_static("account.html", media_type="text/html")


@app.get("/auth.js")
async def auth_js():
    return _serve_static("auth.js", media_type="application/javascript")


@app.get("/auth.css")
async def auth_css():
    return _serve_static("auth.css", media_type="text/css")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    """Public landing page — accessible without login."""
    return _serve_static("landing.html", media_type="text/html")


@app.get("/app", response_class=HTMLResponse)
async def app_index(user: Optional[User] = Depends(optional_user)):
    """Main application — requires authentication.

    Substitutes ``{{V}}`` placeholders in index.html with the current
    build version (see :func:`_build_version`) so any ``?v={{V}}``
    appended to a static asset URL becomes a real cache-busting query
    string. ``no-cache`` on this response forces the browser to revalidate
    the HTML every load, while sub-resources stay long-cached behind
    the version-stamped URL.
    """
    if user is None:
        return RedirectResponse(url="/login?next=%2Fapp", status_code=302)
    from app.config.settings import settings
    with open(os.path.join(_static_dir, "index.html"), encoding="utf-8") as f:
        html = f.read()
    # Read the Vite manifest per request so `npm run dev`'s rolling
    # rebuilds get picked up without restarting uvicorn. Tiny file, OS
    # caches the disk read — negligible overhead.
    js_entry = _vite_entry()
    js_entry_tag = (
        f'<script type="module" src="{js_entry}" defer></script>'
        if js_entry
        else "<!-- frontend bundle not built — `cd web && npm run build` -->"
    )
    html = (
        html
        .replace("{{V}}", _STATIC_VERSION)
        .replace("{{SENTRY_DSN}}", (settings.sentry_dsn or "").strip())
        .replace("{{JS_ENTRY_TAG}}", js_entry_tag)
    )
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})


@app.get("/api/app-version")
async def app_version():
    """Frontend reads this to decide whether to prompt a reload after a
    server-side upgrade. Cheap, no-auth, just the version string."""
    return {"version": _STATIC_VERSION}


@app.get("/health")
async def health():
    return {"ok": True, "service": "research-assistant"}


def _monitor_db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url or "user:password@localhost" in url:
        return ""
    return url


def _monitor_empty_summary() -> dict:
    return {
        "call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "avg_latency_ms": 0,
        "cost_yuan": 0,
        "error_count": 0,
    }


def _monitor_clean(value):
    if isinstance(value, list):
        return [_monitor_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _monitor_clean(item) for key, item in value.items()}
    if hasattr(value, "__float__") and value.__class__.__name__ == "Decimal":
        return float(value)
    return value


def _monitor_fetch(sql: str, params: tuple = (), *, one: bool = False):
    url = _monitor_db_url()
    if not url:
        return None if one else []
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(_MONITOR_INIT_SQL)
                cur.execute(sql, params)
                result = cur.fetchone() if one else cur.fetchall()
                return _monitor_clean(result)
    except Exception as exc:
        logger.warning("Token monitor query failed: %s", exc)
        return None if one else []


@app.get("/monitor/summary")
async def monitor_summary(days: int = Query(7, ge=1, le=90), agent_name: Optional[str] = None,
                          admin: User = Depends(require_admin)):
    where = "created_at >= NOW() - (%s * INTERVAL '1 day')"
    params: list = [days]
    if agent_name:
        where += " AND agent_name = %s"
        params.append(agent_name)
    row = _monitor_fetch(
        f"""
        SELECT
            COUNT(*)::INT AS call_count,
            COALESCE(SUM(prompt_tokens),0)::INT AS prompt_tokens,
            COALESCE(SUM(completion_tokens),0)::INT AS completion_tokens,
            COALESCE(SUM(cache_read_tokens),0)::INT AS cache_read_tokens,
            COALESCE(SUM(total_tokens),0)::INT AS total_tokens,
            COALESCE(ROUND(AVG(latency_ms)),0)::INT AS avg_latency_ms,
            COALESCE(ROUND(SUM(cost_yuan)::NUMERIC, 4),0) AS cost_yuan,
            COUNT(*) FILTER (WHERE status='error')::INT AS error_count
        FROM llm_token_usage
        WHERE {where}
        """,
        tuple(params),
        one=True,
    )
    return row or _monitor_empty_summary()


@app.get("/monitor/daily-trend")
async def monitor_daily_trend(days: int = Query(7, ge=1, le=90), agent_name: Optional[str] = None,
                              admin: User = Depends(require_admin)):
    where = "created_at >= NOW() - (%s * INTERVAL '1 day')"
    params: list = [days]
    if agent_name:
        where += " AND agent_name = %s"
        params.append(agent_name)
    return _monitor_fetch(
        f"""
        SELECT
            DATE(created_at AT TIME ZONE 'Asia/Shanghai')::TEXT AS day,
            COALESCE(SUM(total_tokens),0)::INT AS total_tokens,
            COALESCE(SUM(prompt_tokens),0)::INT AS prompt_tokens,
            COALESCE(SUM(completion_tokens),0)::INT AS completion_tokens,
            COUNT(*)::INT AS call_count,
            COALESCE(ROUND(SUM(cost_yuan)::NUMERIC, 4),0) AS cost_yuan
        FROM llm_token_usage
        WHERE {where}
        GROUP BY 1
        ORDER BY 1 DESC
        """,
        tuple(params),
    )


@app.get("/monitor/model-breakdown")
async def monitor_model_breakdown(days: int = Query(7, ge=1, le=90), agent_name: Optional[str] = None,
                                  admin: User = Depends(require_admin)):
    where = "created_at >= NOW() - (%s * INTERVAL '1 day')"
    params: list = [days]
    if agent_name:
        where += " AND agent_name = %s"
        params.append(agent_name)
    return _monitor_fetch(
        f"""
        SELECT
            provider,
            model,
            COUNT(*)::INT AS call_count,
            COALESCE(SUM(total_tokens),0)::INT AS total_tokens,
            COALESCE(SUM(prompt_tokens),0)::INT AS prompt_tokens,
            COALESCE(SUM(completion_tokens),0)::INT AS completion_tokens,
            COALESCE(ROUND(AVG(latency_ms)),0)::INT AS avg_latency_ms,
            COALESCE(ROUND(SUM(cost_yuan)::NUMERIC, 4),0) AS cost_yuan,
            COUNT(*) FILTER (WHERE status='error')::INT AS error_count
        FROM llm_token_usage
        WHERE {where}
        GROUP BY provider, model
        ORDER BY total_tokens DESC
        LIMIT 20
        """,
        tuple(params),
    )


@app.get("/monitor/hourly")
async def monitor_hourly(days: int = Query(7, ge=1, le=90), agent_name: Optional[str] = None,
                         admin: User = Depends(require_admin)):
    where = "created_at >= NOW() - (%s * INTERVAL '1 day')"
    params: list = [days]
    if agent_name:
        where += " AND agent_name = %s"
        params.append(agent_name)
    rows = _monitor_fetch(
        f"""
        SELECT
            EXTRACT(HOUR FROM created_at AT TIME ZONE 'Asia/Shanghai')::INT AS hour,
            COALESCE(SUM(total_tokens),0)::INT AS total_tokens,
            COUNT(*)::INT AS call_count
        FROM llm_token_usage
        WHERE {where}
        GROUP BY 1
        ORDER BY 1
        """,
        tuple(params),
    )
    by_hour = {row["hour"]: row for row in rows}
    return [by_hour.get(i, {"hour": i, "total_tokens": 0, "call_count": 0}) for i in range(24)]


@app.get("/monitor/agents")
async def monitor_agents(days: int = Query(7, ge=1, le=90),
                         admin: User = Depends(require_admin)):
    return _monitor_fetch(
        """
        SELECT
            agent_name,
            COUNT(*)::INT AS call_count,
            COALESCE(SUM(total_tokens),0)::INT AS total_tokens,
            COALESCE(ROUND(SUM(cost_yuan)::NUMERIC, 4),0) AS cost_yuan
        FROM llm_token_usage
        WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
        GROUP BY agent_name
        ORDER BY total_tokens DESC
        LIMIT 20
        """,
        (days,),
    )


@app.get("/monitor/errors")
async def monitor_errors(days: int = Query(1, ge=1, le=30), limit: int = Query(50, ge=1, le=200),
                         admin: User = Depends(require_admin)):
    return _monitor_fetch(
        """
        SELECT
            (created_at AT TIME ZONE 'Asia/Shanghai')::TEXT AS created_at,
            provider,
            model,
            agent_name,
            task_name,
            latency_ms,
            error_msg
        FROM llm_token_usage
        WHERE status='error'
          AND created_at >= NOW() - (%s * INTERVAL '1 day')
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (days, limit),
    )


@app.get("/api/sessions")
async def list_sessions(user: User = Depends(require_user)):
    """Return sessions visible to *user*.

    Regular users see only sessions they own (`owner_user_id == user.id`).
    Admins see every session — including legacy unowned ones."""
    result: list[dict] = []
    if not os.path.exists(_SESSIONS_DIR):
        return {"sessions": result}

    entries = [
        (fname, os.path.getmtime(os.path.join(_SESSIONS_DIR, fname)))
        for fname in os.listdir(_SESSIONS_DIR)
        if fname.endswith(".json")
    ]
    entries.sort(key=lambda x: x[1], reverse=True)

    for fname, mtime in entries:
        sid = fname[:-5]
        try:
            with open(os.path.join(_SESSIONS_DIR, fname), encoding="utf-8") as f:
                data = json.load(f)
            owner = str(data.get("owner_user_id") or "")
            if not user.is_admin and owner != user.id:
                continue
            turns = data.get("recent_turns", [])
            first_user = next(
                (m["content"] for m in turns if m.get("role") == "user"), ""
            )
            result.append({
                "session_id": sid,
                "title": first_user[:50] if first_user else "新建对话",
                "updated_at": data.get("updated_at", ""),
                "message_count": sum(1 for m in turns if m.get("role") == "user"),
            })
        except Exception:
            pass

    return {"sessions": result}


@app.post("/api/sessions", status_code=201)
async def create_session(user: User = Depends(require_user)):
    """Create and return a new empty session owned by *user*."""
    sid = f"session_{uuid.uuid4().hex[:8]}"
    mem = _get_mem(sid)           # initialise + cache
    _stamp_session_owner(mem, user)
    return {"session_id": sid}


@app.get("/api/sessions/{sid}")
async def get_session_detail(sid: str, user: User = Depends(require_user)):
    """Return a session's conversation history and current paper state."""
    _ensure_session_owner(user, sid)
    mem = _get_mem(sid)
    lib_titles: list[str] = []
    try:
        from app.rag.long_term.store import get_lt_rag_store
        lib_titles = await get_lt_rag_store().list_documents()
    except Exception:
        pass
    return {
        "session_id": sid,
        "conversation_history": mem.short_term.get_full_history(),
        "stored_papers": mem.short_term.stored_papers,
        "found_papers":  mem.short_term.found_papers,
        "library": lib_titles,
        "compression": mem.short_term.compression_status(),
    }


@app.delete("/api/sessions/{sid}", status_code=204)
async def delete_session(sid: str, user: User = Depends(require_user)):
    """Remove a session from memory cache and delete its persisted file."""
    _ensure_session_owner(user, sid)
    _sessions.pop(sid, None)
    path = os.path.join(_SESSIONS_DIR, f"{sid}.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


@app.post("/api/sessions/{sid}/compress")
async def compress_session(sid: str, user: User = Depends(require_user)):
    """Manually compress older messages in one session."""
    _ensure_session_owner(user, sid)
    mem = _get_mem(sid)
    await mem.compress_now()
    mem.save()
    return {
        "session_id": sid,
        "compression": mem.short_term.compression_status(),
        "conversation_history": mem.short_term.get_full_history(),
    }


@app.post("/api/research/{task_id}/resume")
async def research_resume(task_id: str, body: dict, user: User = Depends(require_user)):
    """Resume a paused ResearchAgent plan-approval checkpoint.

    Body schema:
        {"action": "approve" | "modify" | "cancel",
         "modified_plan": {... full plan dict ...}}    # only when action="modify"

    The actual graph resume happens in the executor's wait loop — this
    endpoint just signals the asyncio.Event keyed by task_id.

    Auth: caller must be the user who started the workflow. Without this
    check any logged-in user could cancel / modify someone else's task
    by guessing the task_id.
    """
    from app.orchestrator.research_hitl import get_owner, signal_resume

    action = (body.get("action") or "").lower().strip()
    if action not in {"approve", "modify", "cancel"}:
        raise HTTPException(400, "action must be one of: approve, modify, cancel")

    owner = get_owner(task_id)
    if owner is None:
        # No active checkpoint — most likely it already timed out and
        # auto-approved, or the client is stale. Return 409 so the UI
        # can clear its plan card.
        raise HTTPException(409, "no pending checkpoint for this task")
    if owner and owner != user.id and not user.is_admin:
        # Don't leak whether the checkpoint exists for a different user —
        # respond with the same 404 we'd give a never-existed task.
        raise HTTPException(404, "task not found")

    decision: dict = {"action": action}
    if action == "modify":
        modified = body.get("modified_plan")
        if not isinstance(modified, dict) or not modified.get("steps"):
            raise HTTPException(400, "modify action requires modified_plan with non-empty steps")
        decision["modified_plan"] = modified

    if not signal_resume(task_id, decision):
        raise HTTPException(409, "no pending checkpoint for this task")

    return {"task_id": task_id, "action": action, "status": "signalled"}


@app.get("/api/libraries")
async def list_libraries(user: User = Depends(require_user)):
    """Return libraries visible to *user*.

    Regular users see only libraries they own; admins see every library."""
    from app.rag.long_term.store import get_lt_rag_store
    from app.services.note_service import get_note_service
    await get_note_service().sync_embedded_notes_to_library()
    lt  = get_lt_rag_store()
    libs = lt.list_libraries(owner_user_id=user.id, include_all=user.is_admin)
    result = []
    for lib in libs:
        titles = await lt.list_documents(lib["lib_id"])
        result.append({**lib, "doc_count": len(titles)})
    return {"libraries": result}


@app.get("/api/profile")
async def get_profile(user: User = Depends(require_user)):
    """Return the caller's profile settings."""
    from app.memory.manager import _get_long_term
    return {"profile": _get_long_term().get_profile_settings(user_id=user.id)}


@app.put("/api/profile")
async def update_profile(body: dict, user: User = Depends(require_user)):
    """Update *user*'s profile and index their self-description as long-term memory."""
    from app.memory.manager import _get_long_term

    display_name = str(body.get("display_name") or "").strip()
    avatar = str(body.get("avatar") or "").strip()
    self_description = str(body.get("self_description") or "").strip()

    if len(display_name) > 60:
        raise HTTPException(400, "display_name is too long")
    if len(avatar) > 300_000:
        raise HTTPException(400, "avatar image is too large")
    if len(self_description) > 4000:
        raise HTTPException(400, "self_description is too long")

    lt_mem = _get_long_term()
    profile = lt_mem.update_profile_settings(
        display_name, avatar, self_description, user_id=user.id,
    )
    lt_mem.save()

    if self_description:
        try:
            from app.rag.long_term.store import get_lt_rag_store
            content = (
                f"User profile feature. Name: {profile['display_name']}. "
                f"Self-description: {self_description}"
            )
            await get_lt_rag_store().index_conclusion(
                content=content,
                topic="user_profile",
                session_id="profile_settings",
            )
        except Exception as exc:
            logger.warning("Profile self-description indexing failed: %s", exc)

    return {"profile": profile}


@app.get("/api/llm-config")
async def get_llm_config(user: User = Depends(require_user)):
    """Return per-agent LLM configuration. System-level — visible to any signed-in user."""
    from app.services.llm import get_available_llm_options, load_agent_llm_config
    return {
        "config": load_agent_llm_config(),
        "options": get_available_llm_options(),
        "writable": user.is_admin,
    }


@app.put("/api/llm-config")
async def update_llm_config(body: dict, user: User = Depends(require_user)):
    """Persist per-agent LLM configuration. Admin-only — this changes system behaviour."""
    if not user.is_admin:
        raise HTTPException(403, "only the admin can change LLM configuration")
    from app.services.llm import save_agent_llm_config

    config = body.get("config") or {}
    try:
        saved = save_agent_llm_config(config)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if _orch:
        _orch.reload_llm_config()
        for mem in _sessions.values():
            mem.set_llm(_orch.llm)
    return {"config": saved}


@app.post("/api/llm-config/test")
async def test_llm_config(body: dict, user: User = Depends(require_user)):
    """Test one provider/model pair without saving it. Admin-only."""
    if not user.is_admin:
        raise HTTPException(403, "only the admin can test LLM configuration")
    import time
    from app.services.llm import LLMMessage, get_agent_llm_provider, get_llm_provider

    agent_name = str(body.get("agent_name") or "").strip()
    provider = str(body.get("provider") or "").strip()
    model = str(body.get("model") or "").strip()
    if not provider or not model:
        raise HTTPException(400, "provider and model are required")

    try:
        llm = (
            get_agent_llm_provider(agent_name, provider, model)
            if agent_name
            else get_llm_provider(provider, model)
        )
        start = time.perf_counter()
        resp = await llm.complete(
            messages=[LLMMessage(role="user", content="Reply with exactly: ok")],
            system="You are testing an API connection. Reply with exactly: ok",
            max_tokens=8,
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "success": True,
            "agent_name": agent_name,
            "provider": provider,
            "model": resp.model or model,
            "latency_ms": latency_ms,
            "reply": resp.content.strip()[:120],
        }
    except Exception as exc:
        logger.warning("LLM test failed (%s/%s): %s", provider, model, exc)
        return {
            "success": False,
            "agent_name": agent_name,
            "provider": provider,
            "model": model,
            "error": str(exc)[:1000],
        }


@app.post("/api/academic-writing-chat")
async def academic_writing_chat(body: dict, user: User = Depends(require_user)):
    """Small backend proxy for the academic writing chat page.

    Auth required — this endpoint invokes the configured text LLM and
    would otherwise let anonymous callers burn the API budget.
    """
    from app.services.llm import LLMMessage, get_agent_llm_provider, load_agent_llm_config

    labels = {
        "zh": "中文", "en": "English",
        "academic": "学术", "formal": "正式", "concise": "简洁", "review": "综述",
        "short": "短", "medium": "中", "long": "长",
        "polish": "润色", "rewrite": "改写", "supplement": "补充论述", "imitate": "模仿写作",
    }
    settings_body = body.get("settings") or {}
    lang = labels.get(str(settings_body.get("lang") or "zh"), "中文")
    style = labels.get(str(settings_body.get("style") or "academic"), "学术")
    length = labels.get(str(settings_body.get("length") or "short"), "短")
    mode = labels.get(str(settings_body.get("mode") or "polish"), "润色")
    kb_enabled = bool(settings_body.get("kb"))
    system = f"你是学术写作助手。语言={lang}，风格={style}，篇幅={length}，模式={mode}。简洁输出，直接给结果。"
    if kb_enabled:
        system += " 已启用知识库检索。"

    raw_messages = body.get("messages") or []
    messages: list[LLMMessage] = []
    for item in raw_messages[-20:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append(LLMMessage(role=role, content=content))
    if not messages:
        raise HTTPException(400, "messages are required")

    try:
        cfg = load_agent_llm_config().get("writing_agent", {})
        llm = get_agent_llm_provider("writing_agent", cfg.get("provider"), cfg.get("model"))
        resp = await llm.complete(
            messages=messages,
            system=system,
            max_tokens=1000,
            temperature=0.3,
        )
        return {"reply": resp.content, "model": resp.model or cfg.get("model") or ""}
    except Exception as exc:
        logger.warning("Academic writing chat failed: %s", exc)
        raise HTTPException(502, str(exc)[:1000])


@app.post("/api/stop")
async def stop_generation(body: dict, user: User = Depends(require_user)):
    """Cancel the currently running agent task for a session.

    Auth + ownership check — otherwise anonymous callers could DoS by
    cancelling arbitrary in-flight tasks once they know (or guess) the
    session_id.
    """
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        raise HTTPException(400, "session_id is required")
    _ensure_session_owner(user, sid)
    task = _running_tasks.get(sid)
    if not task or task.done():
        return {"stopped": False, "message": "no running task"}
    task.cancel()
    return {"stopped": True}


@app.post("/api/sessions/{sid}/papers/download")
async def download_session_found_paper(sid: str, body: dict, user: User = Depends(require_user)):
    """Download one paper from the current search results without starting a chat workflow."""
    _ensure_session_owner(user, sid)
    mem = _get_mem(sid)
    paper = _resolve_found_paper(mem.short_term.found_papers or [], body)
    if not paper:
        raise HTTPException(404, "paper not found in current search results")

    existing_index = _stored_paper_index(mem.short_term.stored_papers, paper)
    if existing_index >= 0:
        return {
            "success": True,
            "already_downloaded": True,
            "paper": mem.short_term.stored_papers[existing_index],
            "stored_index": existing_index,
            "stored_papers": mem.short_term.stored_papers,
        }

    tool = ToolRegistry.get("download_pdf")
    result = await tool.execute(papers=[paper])
    downloaded = result.data.get("downloaded_pdfs", []) if result.success else []
    if not downloaded:
        failed = result.data.get("failed", []) if result.success else []
        detail = failed[0].get("error") if failed else (result.error or "download failed")
        raise HTTPException(500, detail)

    downloaded_paper = downloaded[0]
    mem.short_term.stored_papers.append(downloaded_paper)
    mem.save()
    stored_index = len(mem.short_term.stored_papers) - 1
    return {
        "success": True,
        "already_downloaded": False,
        "paper": downloaded_paper,
        "stored_index": stored_index,
        "stored_papers": mem.short_term.stored_papers,
    }


@app.get("/api/sessions/{sid}/papers/file")
async def read_session_paper_file(sid: str, index: int | None = None, title: str = "", user: User = Depends(require_user)):
    """Serve a downloaded session paper for inline reading."""
    _ensure_session_owner(user, sid)
    mem = _get_mem(sid)
    papers = mem.short_term.stored_papers or []
    paper = None
    if index is not None and 0 <= index < len(papers):
        paper = papers[index]
    elif title:
        needle = title.strip().lower()
        paper = next((p for p in papers if str(p.get("title") or "").strip().lower() == needle), None)
    if not paper:
        raise HTTPException(404, "downloaded paper not found")

    local_path = os.path.abspath(str(paper.get("local_path") or ""))
    if not local_path:
        raise HTTPException(404, "paper file path missing")
    if not any(os.path.commonpath([root, local_path]) == root for root in _READABLE_ROOTS):
        raise HTTPException(403, "paper file is outside readable storage")
    if not os.path.exists(local_path):
        raise HTTPException(404, "paper file not found")

    media_type = mimetypes.guess_type(local_path)[0] or "application/pdf"
    return FileResponse(
        local_path,
        filename=os.path.basename(local_path),
        media_type=media_type,
        content_disposition_type="inline",
    )


@app.post("/api/sessions/{sid}/library/add")
async def add_session_papers_to_library(sid: str, body: dict, user: User = Depends(require_user)):
    """Index downloaded session papers into the long-term knowledge base.

    This endpoint intentionally bypasses the chat orchestrator so a user can
    store papers while a long-running reading task continues on the WebSocket.
    """
    _ensure_session_owner(user, sid)
    mem = _get_mem(sid)
    papers = mem.short_term.stored_papers or []
    if not papers:
        raise HTTPException(400, "no downloaded papers in this session")

    lib_id = str(body.get("lib_id") or "lt_docs").strip() or "lt_docs"
    _ensure_library_owner(user, lib_id)
    selected: list[dict] = []

    if body.get("index") is not None:
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            raise HTTPException(400, "index must be an integer")
        if idx < 0 or idx >= len(papers):
            raise HTTPException(404, "paper index not found")
        selected = [papers[idx]]
    elif body.get("title"):
        needle = str(body.get("title") or "").strip().lower()
        selected = [
            p for p in papers
            if needle and needle in str(p.get("title") or "").lower()
        ]
    else:
        selected = papers

    if not selected:
        raise HTTPException(404, "no matching papers found")

    add_tool = ToolRegistry.get("add_to_library")
    added: list[dict] = []
    failed: list[dict] = []

    for paper in selected:
        title = str(paper.get("title") or "Unknown")
        local_path = str(paper.get("local_path") or "")
        if not local_path or not os.path.exists(local_path):
            failed.append({"title": title, "error": "local file not found"})
            continue
        result = await add_tool.execute(
            local_path=local_path,
            title=title,
            lib_id=lib_id,
            extra_meta=_paper_extra_meta(paper),
        )
        if result.success:
            chunks_indexed = int(result.data.get("chunks_indexed", 0) or 0)
            paper["lib_id"] = lib_id
            paper["chunks_indexed"] = chunks_indexed
            added.append({"title": title, "chunks_indexed": chunks_indexed})
        else:
            failed.append({"title": title, "error": result.error or "indexing failed"})

    mem.save()
    return {
        "success": bool(added),
        "added": added,
        "failed": failed,
        "stored_papers": mem.short_term.stored_papers,
    }


@app.post("/api/evaluation/rag")
async def evaluate_rag_sample(body: dict, user: User = Depends(require_user)):
    """Evaluate one RAG answer sample without running the full agent pipeline."""
    from app.services.evaluation import EvaluationService, RAGEvaluationSample

    question = str(body.get("question") or "").strip()
    answer = str(body.get("answer") or "").strip()
    contexts = body.get("contexts") or []
    if not question or not answer or not isinstance(contexts, list) or not contexts:
        raise HTTPException(400, "question, answer and contexts[] are required")

    result = await EvaluationService(body.get("backend")).run(RAGEvaluationSample(
        question=question,
        answer=answer,
        contexts=[str(c) for c in contexts if str(c).strip()],
        metadata=body.get("metadata") or {},
    ))
    return {"evaluation": result}


def _ensure_note_owner(user: User, note_id: str):
    """Return the note if visible to *user*; raise 404 otherwise."""
    from app.services.note_service import get_note_service
    try:
        note = get_note_service().get_note(note_id)
    except KeyError:
        raise HTTPException(404, "note not found")
    if not user.is_admin and note.user_id != user.id:
        raise HTTPException(404, "note not found")
    return note


@app.get("/api/notes")
async def list_notes(q: str = "", tag: str = "", source_type: str = "",
                     user: User = Depends(require_user)):
    from app.services.note_service import get_note_service
    if user.is_admin:
        # Admins see everything; merge a couple of common owner buckets.
        svc = get_note_service()
        seen: dict[str, object] = {}
        for owner in (user.id, "local"):
            for n in svc.list_notes(
                user_id=owner,
                filters={"query": q, "tag": tag, "source_type": source_type},
            ):
                seen[n.id] = n
        notes = sorted(seen.values(), key=lambda n: n.updated_at, reverse=True)
    else:
        notes = get_note_service().list_notes(
            user_id=user.id,
            filters={"query": q, "tag": tag, "source_type": source_type},
        )
    return {"notes": [n.model_dump() for n in notes]}


@app.post("/api/notes", status_code=201)
async def create_note(body: dict, user: User = Depends(require_user)):
    from app.schemas.note_schema import NoteCreate
    from app.services.note_service import get_note_service
    note = get_note_service().create_note(NoteCreate(
        user_id=user.id,
        title=(body.get("title") or "新笔记").strip(),
        content_markdown=body.get("content_markdown") or "",
        source_type=body.get("source_type") or "manual",
        source_id=body.get("source_id") or "",
        paper_id=body.get("paper_id") or "",
        conversation_id=body.get("conversation_id") or "",
        tags=body.get("tags") or [],
        metadata=body.get("metadata") or {},
    ))
    return {"note": note.model_dump()}


@app.get("/api/notes/{note_id}")
async def get_note(note_id: str, user: User = Depends(require_user)):
    note = _ensure_note_owner(user, note_id)
    return {"note": note.model_dump()}


@app.get("/api/notes/{note_id}/export.pdf")
async def export_note_pdf(note_id: str, user: User = Depends(require_user)):
    from app.services.note_export import export_note_to_pdf, safe_pdf_filename
    note = _ensure_note_owner(user, note_id)

    export_dir = os.path.join(".", "data", "exports", "notes")
    output_path = os.path.join(export_dir, f"{note_id}.pdf")
    try:
        export_note_to_pdf(note, output_path)
    except Exception as exc:
        logger.exception("Failed to export note PDF: %s", note_id)
        raise HTTPException(500, f"export failed: {exc}")
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=safe_pdf_filename(note.title),
    )


@app.put("/api/notes/{note_id}")
async def update_note(note_id: str, body: dict, user: User = Depends(require_user)):
    _ensure_note_owner(user, note_id)
    from app.schemas.note_schema import NoteUpdate
    from app.services.note_service import get_note_service
    try:
        note = get_note_service().update_note(note_id, NoteUpdate(**body))
    except KeyError:
        raise HTTPException(404, "note not found")
    return {"note": note.model_dump()}


@app.delete("/api/notes/{note_id}", status_code=204)
async def delete_note(note_id: str, user: User = Depends(require_user)):
    note = _ensure_note_owner(user, note_id)
    from app.services.note_service import get_note_service
    logger.warning(
        "Delete note requested: note_id=%s note_user=%s request_user=%s is_admin=%s",
        note_id, note.user_id, user.id, user.is_admin,
    )
    if not get_note_service().delete_note(note_id):
        logger.warning("Delete note failed/not found: note_id=%s", note_id)
        raise HTTPException(404, "note not found")
    logger.warning("Delete note succeeded: note_id=%s", note_id)


@app.post("/api/notes/{note_id}/embed")
async def embed_note(note_id: str, user: User = Depends(require_user)):
    _ensure_note_owner(user, note_id)
    from app.services.note_service import get_note_service
    try:
        result = await get_note_service().embed_note(note_id)
    except KeyError:
        raise HTTPException(404, "note not found")
    return result


@app.get("/api/day-tasks")
async def list_day_tasks(date: str = "", month: str = "",
                         user: User = Depends(require_user)):
    from app.services.calendar_service import get_daily_schedule_service
    svc = get_daily_schedule_service()
    if user.is_admin:
        # Admins see their own tasks plus anything still tagged 'local'.
        merged = {}
        for owner in (user.id, "local"):
            for t in svc.list_tasks(user_id=owner, date=date, month=month):
                merged[t.id] = t
        tasks = sorted(merged.values(), key=lambda t: (t.task_date, t.start_time, t.created_at))
    else:
        tasks = svc.list_tasks(user_id=user.id, date=date, month=month)
    return {"tasks": [task.model_dump() for task in tasks]}


def _ensure_day_task_owner(user: User, task_id: str):
    """Return the task if visible to *user*; raise 404 otherwise."""
    from app.services.calendar_service import get_daily_schedule_service
    try:
        task = get_daily_schedule_service().get_task(task_id)
    except KeyError:
        raise HTTPException(404, "day task not found")
    if not user.is_admin and task.user_id != user.id:
        raise HTTPException(404, "day task not found")
    return task


@app.post("/api/day-tasks", status_code=201)
async def create_day_task(body: dict, user: User = Depends(require_user)):
    from app.schemas.calendar import DayTaskCreate
    from app.services.calendar_service import get_daily_schedule_service

    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    task_date = str(body.get("task_date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", task_date):
        raise HTTPException(400, "task_date must be YYYY-MM-DD")
    task = get_daily_schedule_service().create_task(DayTaskCreate(
        user_id=user.id,
        task_date=task_date,
        start_time=body.get("start_time") or "09:00",
        end_time=body.get("end_time") or "10:00",
        title=title,
        notes=body.get("notes") or "",
        remind=bool(body.get("remind", False)),
        completed=bool(body.get("completed", False)),
        metadata=body.get("metadata") or {},
    ))
    return {"task": task.model_dump()}


@app.put("/api/day-tasks/{task_id}")
async def update_day_task(task_id: str, body: dict, user: User = Depends(require_user)):
    _ensure_day_task_owner(user, task_id)
    from app.schemas.calendar import DayTaskUpdate
    from app.services.calendar_service import get_daily_schedule_service
    try:
        task = get_daily_schedule_service().update_task(task_id, DayTaskUpdate(**body))
    except KeyError:
        raise HTTPException(404, "day task not found")
    return {"task": task.model_dump()}


@app.delete("/api/day-tasks/{task_id}", status_code=204)
async def delete_day_task(task_id: str, user: User = Depends(require_user)):
    _ensure_day_task_owner(user, task_id)
    from app.services.calendar_service import get_daily_schedule_service
    if not get_daily_schedule_service().delete_task(task_id):
        raise HTTPException(404, "day task not found")


@app.post("/api/libraries", status_code=201)
async def create_library(body: dict, user: User = Depends(require_user)):
    """Create a new named knowledge base owned by the current user."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    from app.rag.long_term.store import get_lt_rag_store
    lib_id = get_lt_rag_store().create_library(name, owner_user_id=user.id)
    return {"lib_id": lib_id, "name": name}


@app.delete("/api/libraries/{lib_id}", status_code=204)
async def delete_library(lib_id: str, user: User = Depends(require_user)):
    """Delete a knowledge base and all its documents."""
    _ensure_library_owner(user, lib_id)
    from app.rag.long_term.store import get_lt_rag_store
    try:
        await get_lt_rag_store().delete_library(lib_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/figure/upload")
async def upload_figure_paper(
    file: UploadFile,
    session_id: str = "",
    user: User = Depends(require_user),
):
    """Upload and parse a paper for figure-prompt generation."""
    if not session_id:
        raise HTTPException(400, "session_id query parameter is required")
    _ensure_session_owner(user, session_id)

    filename = file.filename or "paper.pdf"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".pdf", ".txt", ".md", ".text", ".rst"}:
        raise HTTPException(400, "Only PDF/TXT/MD files are supported for figure generation")

    content = await file.read()
    from app.services.storage_service import save_upload
    _record, dest = save_upload(
        user=user, content=content, original_name=filename, category="figure",
    )

    try:
        from app.tools.pdf.backends import extract_any
        data = await asyncio.to_thread(extract_any, dest)
    except Exception as exc:
        logger.exception("Figure paper extraction failed: %s", filename)
        raise HTTPException(500, f"paper extraction failed: {exc}")

    paper_id = f"fig_{uuid.uuid4().hex[:10]}"
    full_text = data.get("full_text", "")
    sections = data.get("sections", {})
    _figure_docs[paper_id] = {
        "session_id": session_id,
        "filename": filename,
        "local_path": dest,
        "full_text": full_text,
        "sections": sections,
        "metadata": data.get("metadata", {}),
    }
    return {
        "paper_id": paper_id,
        "filename": filename,
        "sections": list(sections.keys())[:20],
        "char_count": len(full_text),
    }


@app.post("/api/figure/prompt")
async def generate_figure_prompt(body: dict, user: User = Depends(require_user)):
    """Use the text LLM to read paper context + user needs and produce an editable image prompt."""
    paper_id = str(body.get("paper_id") or "").strip()
    user_brief = str(body.get("brief") or "").strip()
    hidden_brief_prompt = str(body.get("hidden_brief_prompt") or "").strip()
    figure_type = str(body.get("figure_type") or "method").strip()
    style = str(body.get("style") or "paper").strip()
    doc = _figure_docs.get(paper_id, {}) if paper_id else {}
    paper_context = _figure_paper_context(doc)

    if not user_brief and not paper_context:
        raise HTTPException(400, "brief or uploaded paper is required")

    from app.config.settings import settings
    from app.services.llm import LLMMessage, get_llm_provider
    llm = get_llm_provider(
        settings.figure_prompt_llm_provider or settings.llm_provider,
        settings.figure_prompt_llm_model or None,
    )

    system = (
        "You are a scientific figure prompt engineer. Read the paper context and user requirement, "
        "then write a high-quality prompt for an image generation model. Output only the prompt text. "
        "The prompt should describe layout, visual elements, labels, data flow, style, and constraints. "
        "Do not invent paper-specific details not supported by the context. "
        "Do not include markdown headings, explanations, rationale, quotes, or meta comments such as "
        "'this prompt ensures'. Keep it concise and directly drawable."
    )
    user = (
        f"Figure type: {figure_type}\n"
        f"Style: {style}\n"
        f"Hidden requirement guidance:\n{hidden_brief_prompt or 'No additional hidden guidance.'}\n\n"
        f"User requirement: {user_brief or 'Generate a clear scientific figure from the paper.'}\n\n"
        f"Paper context:\n{paper_context}"
    )
    try:
        resp = await llm.complete(
            messages=[LLMMessage(role="user", content=user)],
            system=system,
            max_tokens=1200,
        )
        prompt_text = resp.content.strip()
    except Exception as exc:
        logger.exception("Figure prompt generation failed")
        raise HTTPException(500, f"prompt generation failed: {exc}")

    return {"prompt": prompt_text, "model": resp.model}


@app.post("/api/figure/generate")
async def generate_figure_image(body: dict, user: User = Depends(require_user)):
    """Generate a real raster image from a prompt and return a displayable image URL."""
    prompt_text = str(body.get("prompt") or "").strip()
    negative = str(body.get("negative") or "").strip()
    ratio = str(body.get("ratio") or "4:3").strip()
    if not prompt_text:
        raise HTTPException(400, "prompt is required")

    from app.config.settings import settings
    try:
        image_bytes, media_type, model = await _generate_raster_figure(
            prompt_text=prompt_text,
            negative=negative,
            ratio=ratio,
            provider=settings.figure_image_provider or "openai",
            model=settings.figure_image_model or "gpt-image-1",
            base_url=settings.figure_image_base_url,
        )
    except Exception as exc:
        logger.exception("Figure image generation failed")
        raise HTTPException(500, f"image generation failed: {exc}")

    if not image_bytes:
        raise HTTPException(500, "image model did not return image data")

    os.makedirs(_FIGURE_OUTPUT_DIR, exist_ok=True)
    image_id = f"figimg_{uuid.uuid4().hex[:10]}"
    ext = {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/png": ".png",
    }.get(media_type, ".png")
    path = os.path.join(_FIGURE_OUTPUT_DIR, f"{image_id}{ext}")
    with open(path, "wb") as f:
        f.write(image_bytes)

    return {
        "image_id": image_id,
        "image_url": f"/api/figure/images/{image_id}{ext}",
        "download_url": f"/api/figure/images/{image_id}{ext}",
        "media_type": media_type,
        "model": model,
    }


@app.get("/api/figure/images/{image_name}")
async def download_figure_image(image_name: str):
    ext = os.path.splitext(image_name)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "only generated image files are supported")
    safe = os.path.basename(image_name)
    path = os.path.abspath(os.path.join(_FIGURE_OUTPUT_DIR, safe))
    root = os.path.abspath(_FIGURE_OUTPUT_DIR)
    if os.path.commonpath([root, path]) != root or not os.path.exists(path):
        raise HTTPException(404, "image not found")
    return FileResponse(path, filename=safe, media_type=mimetypes.guess_type(path)[0] or "image/png")


@app.get("/api/libraries/{lib_id}/documents")
async def list_library_documents(lib_id: str, user: User = Depends(require_user)):
    _ensure_library_owner(user, lib_id)
    from app.rag.long_term.store import get_lt_rag_store
    from app.services.note_service import get_note_service
    if lib_id == "lt_docs":
        await get_note_service().sync_embedded_notes_to_library()
    lt = get_lt_rag_store()
    documents = await lt.list_document_records(lib_id)
    return {"lib_id": lib_id, "titles": [d["title"] for d in documents], "documents": documents}


@app.get("/api/libraries/{lib_id}/documents/file")
async def read_library_document(lib_id: str, title: str, user: User = Depends(require_user)):
    _ensure_library_owner(user, lib_id)
    """Serve the original file for a long-term library document."""
    from app.rag.long_term.store import get_lt_rag_store
    from app.services.note_service import get_note_service
    import html

    documents = await get_lt_rag_store().list_document_records(lib_id)
    doc = next((d for d in documents if d.get("title") == title), None)
    if not doc:
        raise HTTPException(404, "document not found")

    source = doc.get("source") or ""
    if not source:
        raise HTTPException(404, "document source is missing")

    if source.startswith("note://"):
        note_id = source.split("note://", 1)[1]
        try:
            note = get_note_service().get_note(note_id)
        except KeyError:
            raise HTTPException(404, "note not found")
        safe_title = html.escape(note.title)
        safe_content = html.escape(note.content_markdown or "")
        return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;margin:0;background:#f8fafc;color:#172033}}
    main{{max-width:920px;margin:0 auto;padding:28px 32px}}
    h1{{font-size:24px;line-height:1.35;margin:0 0 18px}}
    pre{{white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;line-height:1.72;font-family:inherit;font-size:14px}}
  </style>
</head>
<body><main><h1>{safe_title}</h1><pre>{safe_content}</pre></main></body>
</html>""")

    abs_source = os.path.abspath(source)
    if not any(os.path.commonpath([root, abs_source]) == root for root in _READABLE_ROOTS):
        raise HTTPException(403, "document source is outside readable storage")
    if not os.path.exists(abs_source):
        raise HTTPException(404, "document file not found")

    media_type = mimetypes.guess_type(abs_source)[0] or "application/octet-stream"
    return FileResponse(
        abs_source,
        filename=os.path.basename(abs_source),
        media_type=media_type,
        content_disposition_type="inline",
    )


@app.get("/api/libraries/{lib_id}/documents/chunks")
async def list_library_document_chunks(lib_id: str, title: str, limit: int = 500,
                                        user: User = Depends(require_user)):
    """Return the actual indexed chunks for one long-term library document."""
    _ensure_library_owner(user, lib_id)
    title = (title or "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    if limit < 1 or limit > 2000:
        raise HTTPException(400, "limit must be between 1 and 2000")

    from app.rag.long_term.store import get_lt_rag_store
    lt = get_lt_rag_store()
    documents = await lt.list_document_records(lib_id)
    doc = next((d for d in documents if d.get("title") == title), None)
    if not doc:
        raise HTTPException(404, "document not found")

    chunks = await lt.list_document_chunks(title=title, lib_id=lib_id, limit=limit)
    return {
        "lib_id": lib_id,
        "title": title,
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


@app.post("/api/library_qa")
async def library_qa(body: dict, user: User = Depends(require_user)):
    """Ask one indexed library document via rag_agent.

    The frontend supplies only paper_id/question/session_id. PDF extraction and
    chunk retrieval remain backend responsibilities.
    """
    from app.state.task_state import TaskState

    paper_id = str(body.get("paper_id") or body.get("paperId") or "").strip()
    question = str(body.get("question") or "").strip()
    session_id = str(body.get("session_id") or "").strip() or f"reading_{uuid.uuid4().hex[:8]}"
    if not paper_id:
        raise HTTPException(400, "paper_id is required")
    if not question:
        raise HTTPException(400, "question is required")
    if not _orch:
        raise HTTPException(503, "orchestrator is not ready")

    doc = await _resolve_library_qa_document(paper_id)
    if not doc:
        raise HTTPException(404, "paper is not indexed in the knowledge base")
    # Ensure the target library belongs to the caller (or admin).
    _ensure_library_owner(user, doc.get("lib_id") or "lt_docs")

    mem = _get_mem(session_id)
    scoped_question = f"针对《{doc['title']}》：{question}"
    state = TaskState(user_goal=scoped_question, session_id=session_id, workflow="question_answer_workflow")
    state.working_memory["library_qa_mode"] = True
    state.working_memory["current_library_context"] = mem.short_term.current_library_context
    state.agent_outputs["intent_agent"] = {
        "result": {
            "workflow": "question_answer_workflow",
            "user_intent": "library_qa",
        }
    }

    use_cached = (
        bool(mem.short_term.current_library_context.get("contexts"))
        and _same_library_qa_target(mem.short_term.current_library_context, doc)
    )

    from app.schemas.agent import AgentInput, AgentStatus

    rag_input = AgentInput(
        task_id=state.task_id,
        session_id=session_id,
        agent_name="rag_agent",
        user_goal=scoped_question,
        current_stage="question_answer",
        input_data={
            "documents": [],
            "question": scoped_question,
            "mode": "library_qa",
            "cached_library_context": mem.short_term.current_library_context if use_cached else {},
        },
    )
    rag_output = await _orch._agents["rag_agent"].run(rag_input, state)
    state.record_agent_output("rag_agent", rag_output.model_dump())
    if rag_output.status == AgentStatus.FAILED:
        message = rag_output.errors[0] if rag_output.errors else "rag failed"
        return {
            "session_id": session_id,
            "answer": message,
            "sources": [],
            "paper": doc,
        }

    retrieval_result = state.agent_outputs.get("retrieval_agent", {}).get("result", {})
    reading_result = state.agent_outputs.get("reading_agent", {}).get("result", {})
    rag_result = rag_output.result
    if retrieval_result:
        mem.short_term.current_library_context = {
            "contexts": retrieval_result.get("contexts", []),
            "paper_list": retrieval_result.get("paper_list", ""),
            "lib_names": retrieval_result.get("lib_names", []),
            "active_title": retrieval_result.get("active_title", doc["title"]),
            "question": retrieval_result.get("question", scoped_question),
            "original_question": retrieval_result.get("original_question", scoped_question),
            "title_filter": retrieval_result.get("title_filter", doc["title"]),
            "metadata": retrieval_result.get("metadata", {}),
        }
    answer = reading_result.get("answer") or (
        reading_result.get("reading_notes", [{}])[0].get("answer", "")
        if reading_result.get("reading_notes") else ""
    )
    mem.update_after_turn(question, answer)
    mem.save()
    return {
        "session_id": session_id,
        "answer": answer,
        "sources": _library_qa_sources(retrieval_result, reading_result),
        "paper": doc,
    }


async def _resolve_library_qa_document(paper_id: str) -> dict | None:
    from app.rag.long_term.store import get_lt_rag_store

    lt = get_lt_rag_store()
    records: list[dict] = []
    for lib in lt.list_libraries():
        for rec in await lt.list_document_records(lib["lib_id"]):
            records.append({
                **rec,
                "lib_id": rec.get("lib_id") or lib["lib_id"],
                "lib_name": lib.get("name") or lib["lib_id"],
            })
    if not records:
        return None

    decoded = paper_id.strip()
    if ":" in decoded:
        lib_id, title = decoded.split(":", 1)
        hit = next(
            (r for r in records if r.get("lib_id") == lib_id and r.get("title") == title),
            None,
        )
        if hit:
            return hit

    needle = _paper_id_norm(decoded)
    for rec in records:
        aliases = [
            rec.get("title", ""),
            rec.get("source", ""),
            os.path.basename(str(rec.get("source") or "")),
            f"{rec.get('lib_id', '')}:{rec.get('title', '')}",
        ]
        if any(_paper_id_norm(alias) == needle for alias in aliases if alias):
            return rec
    return None


def _paper_id_norm(value: str) -> str:
    text = str(value or "").lower().replace("\\", "/")
    text = os.path.basename(text)
    text = re.sub(r"\.(pdf|pptx|txt|md|docx?)$", "", text, flags=re.I)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def _same_library_qa_target(cache: dict, doc: dict) -> bool:
    target = str(doc.get("title") or "").strip()
    return bool(target and (
        cache.get("active_title") == target
        or cache.get("title_filter") == target
        or cache.get("paper_list") == target
    ))


def _library_qa_sources(retrieval_result: dict, reading_result: dict) -> list[dict]:
    chunks = retrieval_result.get("retrieved_chunks") or []
    if not chunks:
        contexts = reading_result.get("contexts") or []
        return [
            {"chunk_id": f"context_{idx + 1}", "page": None, "snippet": str(ctx)[:260]}
            for idx, ctx in enumerate(contexts[:5])
        ]
    sources: list[dict] = []
    for idx, chunk in enumerate(chunks[:5]):
        meta = chunk.get("metadata", {}) or {}
        sources.append({
            "chunk_id": chunk.get("id") or chunk.get("chunk_id") or f"chunk_{idx + 1}",
            "page": meta.get("page") or meta.get("page_number"),
            "section": meta.get("section", ""),
            "snippet": str(chunk.get("document") or "")[:260],
        })
    return sources


def _annot_visible(user: User, ann: dict) -> bool:
    """Admin sees everything; otherwise the annotation's user_id must match."""
    if user.is_admin:
        return True
    return (ann.get("user_id") or "") == user.id


@app.get("/api/reading/annotations")
async def list_reading_annotations(doc_id: str, user: User = Depends(require_user)):
    doc_id = (doc_id or "").strip()
    if not doc_id:
        raise HTTPException(400, "doc_id is required")
    items = [
        a for a in _reading_annotations()
        if a.get("doc_id") == doc_id and _annot_visible(user, a)
    ]
    items.sort(key=lambda a: (int(a.get("page") or 0), a.get("created_at") or ""))
    return {"doc_id": doc_id, "annotations": items}


@app.post("/api/reading/annotations")
async def create_reading_annotation(body: dict, user: User = Depends(require_user)):
    doc_id = str(body.get("doc_id") or "").strip()
    selected_text = str(body.get("selected_text") or "").strip()
    rects = body.get("rects") or []
    if not doc_id:
        raise HTTPException(400, "doc_id is required")
    if not selected_text:
        raise HTTPException(400, "selected_text is required")
    if not isinstance(rects, list) or not rects:
        raise HTTPException(400, "rects are required")

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": f"ann_{uuid.uuid4().hex[:12]}",
        "user_id": user.id,
        "doc_id": doc_id,
        "title": str(body.get("title") or ""),
        "source_url": str(body.get("source_url") or ""),
        "page": int(body.get("page") or 1),
        "type": str(body.get("type") or "highlight"),
        "color": str(body.get("color") or "yellow"),
        "selected_text": selected_text[:8000],
        "note": str(body.get("note") or "")[:8000],
        "rects": rects,
        "created_at": now,
        "updated_at": now,
    }
    data = _reading_annotations()
    data.append(item)
    _write_json_file(_ANNOTATIONS_FILE, data)
    return {"annotation": item}


@app.patch("/api/reading/annotations/{annotation_id}")
async def update_reading_annotation(annotation_id: str, body: dict,
                                     user: User = Depends(require_user)):
    data = _reading_annotations()
    item = next((a for a in data if a.get("id") == annotation_id), None)
    if not item or not _annot_visible(user, item):
        raise HTTPException(404, "annotation not found")

    from datetime import datetime, timezone

    for key in ("note", "color", "type"):
        if key in body:
            item[key] = str(body.get(key) or "")
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json_file(_ANNOTATIONS_FILE, data)
    return {"annotation": item}


@app.delete("/api/reading/annotations/{annotation_id}", status_code=204)
async def delete_reading_annotation(annotation_id: str, user: User = Depends(require_user)):
    data = _reading_annotations()
    item = next((a for a in data if a.get("id") == annotation_id), None)
    if not item or not _annot_visible(user, item):
        raise HTTPException(404, "annotation not found")
    next_data = [a for a in data if a.get("id") != annotation_id]
    _write_json_file(_ANNOTATIONS_FILE, next_data)


def _progress_key(user_id: str, doc_id: str) -> str:
    """Composite key keeps each user's reading position separate per doc."""
    return f"{user_id}::{doc_id}"


@app.get("/api/reading/progress")
async def get_reading_progress(doc_id: str, user: User = Depends(require_user)):
    doc_id = (doc_id or "").strip()
    if not doc_id:
        raise HTTPException(400, "doc_id is required")
    data = _reading_progress()
    record = data.get(_progress_key(user.id, doc_id)) or {}
    return {"doc_id": doc_id, "progress": record}


@app.post("/api/reading/progress")
async def save_reading_progress(body: dict, user: User = Depends(require_user)):
    doc_id = str(body.get("doc_id") or "").strip()
    if not doc_id:
        raise HTTPException(400, "doc_id is required")

    from datetime import datetime, timezone

    data = _reading_progress()
    record = {
        "user_id": user.id,
        "doc_id": doc_id,
        "page": max(1, int(body.get("page") or 1)),
        "scale": float(body.get("scale") or 1),
        "scroll_top": max(0, int(body.get("scroll_top") or 0)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    data[_progress_key(user.id, doc_id)] = record
    _write_json_file(_READING_PROGRESS_FILE, data)
    return {"doc_id": doc_id, "progress": record}


@app.post("/api/reading/translate")
async def translate_reading_selection(body: dict, user: User = Depends(require_user)):
    text = str(body.get("text") or "").strip()
    target_lang = str(body.get("target_lang") or "中文").strip() or "中文"
    title = str(body.get("title") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 8000:
        raise HTTPException(400, "text is too long; select a shorter passage")

    from app.services.llm import LLMMessage, get_agent_llm_provider, load_agent_llm_config

    cfg = load_agent_llm_config().get("reading_agent", {})
    llm = get_agent_llm_provider("reading_agent", cfg.get("provider"), cfg.get("model"))
    system = (
        "你是科研论文阅读翻译助手。请把用户选中的论文原文准确翻译为目标语言。\n"
        "要求：保留术语、变量名、缩写、引用编号和公式符号；不要扩写，不要点评；"
        "如果原文已经是目标语言，则做轻微润色并保持原意。"
    )
    prompt = (
        f"目标语言：{target_lang}\n"
        f"论文标题：{title or '未知'}\n\n"
        f"选中文本：\n{text}"
    )
    try:
        resp = await llm.complete(
            [LLMMessage(role="user", content=prompt)],
            system=system,
            temperature=0.2,
            max_tokens=1800,
        )
    except Exception as exc:
        logger.exception("Reading translation failed: %s", exc)
        raise HTTPException(502, f"translation failed: {exc}")
    return {
        "translation": resp.content.strip(),
        "model": resp.model,
        "target_lang": target_lang,
    }


@app.delete("/api/libraries/{lib_id}/documents", status_code=204)
async def delete_library_document(lib_id: str, body: dict, user: User = Depends(require_user)):
    """Remove one document. Body: {\"title\": \"...\"}"""
    _ensure_library_owner(user, lib_id)
    title = (body.get("title") or "").strip()
    source_filter = (body.get("source") or "").strip()
    if not title and not source_filter:
        raise HTTPException(400, "title or source is required")
    from app.rag.long_term.store import get_lt_rag_store
    from app.services.note_service import get_note_service
    lt = get_lt_rag_store()
    documents = await lt.list_document_records(lib_id)
    doc = next(
        (
            d for d in documents
            if (source_filter and d.get("source") == source_filter)
            or (title and d.get("title") == title)
        ),
        None,
    )
    source = (doc or {}).get("source", "")
    if source.startswith("note://"):
        note_id = source.split("note://", 1)[1]
        try:
            await get_note_service().unembed_note(note_id)
        except KeyError:
            await lt.remove_document_source(source, lib_id=lib_id)
        return
    if source_filter:
        await lt.remove_document_source(source_filter, lib_id=lib_id)
        return
    await lt.remove_document(title=title, lib_id=lib_id)


@app.post("/api/upload")
async def upload_file(
    file: UploadFile,
    session_id: str = "",
    lib_id: str = "",
    chunk_size: int = 0,
    chunk_overlap: int = -1,
    user: User = Depends(require_user),
):
    """Accept a PDF or text file, save it, and register it in the session."""
    if not session_id:
        raise HTTPException(400, "session_id query parameter is required")
    _ensure_session_owner(user, session_id)

    filename = file.filename or "document"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            400, f"Unsupported file type '{ext}'. Allowed: {sorted(_ALLOWED_EXTS)}"
        )

    content = await file.read()
    from app.services.storage_service import save_upload
    _record, dest = save_upload(
        user=user, content=content, original_name=filename, category="upload"
    )

    chunks_indexed = 0
    embed_error    = ""
    requested_chunk_size = chunk_size or None
    requested_chunk_overlap = chunk_overlap if chunk_overlap >= 0 else None
    if requested_chunk_size is not None and (requested_chunk_size < 200 or requested_chunk_size > 4000):
        raise HTTPException(400, "chunk_size must be between 200 and 4000")
    if requested_chunk_overlap is not None:
        effective_size = requested_chunk_size or 2000
        if requested_chunk_overlap < 0 or requested_chunk_overlap >= effective_size:
            raise HTTPException(400, "chunk_overlap must be >= 0 and smaller than chunk_size")

    # If a lib_id is specified, embed immediately into that knowledge base
    if lib_id:
        _ensure_library_owner(user, lib_id)
        try:
            from app.rag.long_term.store import get_lt_rag_store
            chunks_indexed = await get_lt_rag_store().add_document(
                local_path=dest,
                title=filename,
                lib_id=lib_id,
                extra_meta={"source_type": "upload"},
                chunk_size=requested_chunk_size,
                chunk_overlap=requested_chunk_overlap,
            )
            logger.info(
                "Embedded '%s' → %d chunks in library '%s'", filename, chunks_indexed, lib_id
            )
        except Exception as exc:
            embed_error = str(exc)
            logger.exception("Embedding failed for '%s'", filename)

    # Always register in session so the reading agent can also read it directly
    mem   = _get_mem(session_id)
    paper = {
        "title": filename,
        "local_path": dest,
        "source": "upload",
        "lib_id": lib_id or "",
        "chunks_indexed": chunks_indexed,
        "chunk_size": requested_chunk_size or 2000,
        "chunk_overlap": requested_chunk_overlap if requested_chunk_overlap is not None else 200,
        "year": "",
        "authors": [],
        "abstract": "",
        "venue": "",
        "journal": "",
    }
    existing = {p.get("local_path") for p in mem.short_term.stored_papers}
    if dest not in existing:
        mem.short_term.stored_papers.append(paper)
    mem.save()

    logger.info("Uploaded '%s' → %s (session=%s, lib=%s)", filename, dest, session_id, lib_id)
    return {
        "success": True,
        "paper": paper,
        "stored_papers": mem.short_term.stored_papers,
        "chunks_indexed": chunks_indexed,
        "chunk_size": requested_chunk_size or 2000,
        "chunk_overlap": requested_chunk_overlap if requested_chunk_overlap is not None else 200,
        "embed_error": embed_error,
    }


@app.post("/api/image/upload")
async def upload_image(
    file: UploadFile,
    session_id: str = "",
    user: User = Depends(require_user),
):
    """Accept an image for the image understanding tool."""
    if not session_id:
        raise HTTPException(400, "session_id query parameter is required")
    _ensure_session_owner(user, session_id)

    filename = file.filename or "image"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise HTTPException(
            400, f"Unsupported image type '{ext}'. Allowed: {sorted(_ALLOWED_IMAGE_EXTS)}"
        )

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "image is too large; max size is 10MB")

    from app.services.storage_service import save_upload
    _record, dest = save_upload(
        user=user, content=content, original_name=filename, category="image",
    )
    safe_name = os.path.relpath(dest, _IMAGE_UPLOAD_DIR).replace(os.sep, "/")

    return {
        "success": True,
        "filename": filename,
        "image_path": dest,
        "image_url": f"/api/image/uploads/{safe_name}",
    }


@app.get("/api/image/uploads/{image_path:path}")
async def get_uploaded_image(image_path: str):
    """Serve uploaded image. *image_path* may include a `{user_id}/` segment."""
    ext = os.path.splitext(image_path)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, "unsupported image type")
    # Normalise + clamp to the image upload root to block traversal
    norm = image_path.replace("\\", "/").lstrip("/")
    path = os.path.abspath(os.path.join(_IMAGE_UPLOAD_DIR, norm))
    root = os.path.abspath(_IMAGE_UPLOAD_DIR)
    if os.path.commonpath([root, path]) != root or not os.path.exists(path):
        raise HTTPException(404, "image not found")
    return FileResponse(path, filename=os.path.basename(path), media_type=mimetypes.guess_type(path)[0] or "image/png")


@app.post("/api/image/analyze")
async def analyze_uploaded_image(body: dict, user: User = Depends(require_user)):
    image_path = str(body.get("image_path") or "").strip()
    if not image_path:
        raise HTTPException(400, "image_path is required")
    safe_path = _resolve_uploaded_image_path(image_path)
    tool = ToolRegistry.get("analyze_image_tool")
    result = await tool.execute(
        image_path=safe_path,
        user_question=str(body.get("user_question") or "").strip() or None,
        task_type=str(body.get("task_type") or "auto").strip() or "auto",
        use_ocr=bool(body.get("use_ocr", True)),
        use_vlm=bool(body.get("use_vlm", True)),
        vlm_provider=body.get("vlm_provider") or None,
        vlm_model_name=body.get("vlm_model_name") or None,
    )
    if not result.success:
        raise HTTPException(500, result.error or "image analysis failed")
    return result.data


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    user = await require_user_ws(ws)
    if user is None:
        # 4401 is a custom close code the frontend treats as "redirect to login"
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    logger.info("WebSocket connected for user %s", user.id)
    try:
        while True:
            raw  = await ws.receive_text()
            payload   = json.loads(raw)
            user_text = payload.get("message", "").strip()
            sid       = payload.get("session_id", "")
            image_path = str(payload.get("image_path") or "").strip()
            if not user_text or not sid:
                continue

            # Ownership: legacy/unstamped sessions get claimed for the current user;
            # otherwise must match (admins bypass).
            owner = _session_owner_id(sid)
            if owner and not user.is_admin and owner != user.id:
                await ws.send_text(json.dumps({"type": "error", "text": "无权访问该会话"}))
                continue

            async def send_progress(payload: dict):
                # Side-channel events (currently ResearchAgent's plan
                # checkpoint) carry a `data` block with its own `type`;
                # forward those as a dedicated WS message so the chat UI
                # can render a plan card instead of a progress line.
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, dict) and data.get("type") == "research_plan_checkpoint":
                    await ws.send_text(json.dumps({"type": "research_plan_checkpoint", **data}))
                    return
                await ws.send_text(json.dumps({"type": "status", **payload}))

            await send_progress({"step": "start", "text": "接收问题，准备分析任务", "pct": 5})
            mem = _get_mem(sid)
            if not mem.short_term.owner_user_id:
                _stamp_session_owner(mem, user)
            run_text = user_text

            if image_path:
                await send_progress({"step": "image", "text": "准备图片理解工作流", "pct": 8})
                safe_image_path = _resolve_uploaded_image_path(image_path)
                run_text = f"{user_text}\n\nIMAGE_PATH={safe_image_path}"

            task = None
            try:
                task = asyncio.create_task(_orch.run(
                    run_text,
                    session_id=sid,
                    conversation_history=mem.short_term.get_full_history(),
                    stored_papers=mem.short_term.stored_papers,
                    found_papers=mem.short_term.found_papers,
                    memory_manager=mem,
                    progress_callback=send_progress,
                ))
                _running_tasks[sid] = task
                state = await task
                await send_progress({"step": "evaluation", "text": "进行回答质量评测", "pct": 94})
                response = await _build_response(state, mem)
                _update_state(state, user_text, response, mem)
                await send_progress({"step": "done", "text": "完成", "pct": 100})
                await ws.send_text(json.dumps({"type": "reply", **response}))

            except asyncio.CancelledError:
                logger.info("Cancelled generation for session %s", sid)
                await ws.send_text(json.dumps({"type": "stopped", "text": "已停止回复"}))
            except Exception as exc:
                logger.exception("Error processing '%s'", user_text)
                await ws.send_text(json.dumps({"type": "error", "text": str(exc)}))
            finally:
                if task is not None and _running_tasks.get(sid) is task:
                    _running_tasks.pop(sid, None)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_uploaded_image_path(image_path: str) -> str:
    path = os.path.abspath(image_path)
    root = os.path.abspath(_IMAGE_UPLOAD_DIR)
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(400, "image_path is outside uploaded image directory")
    if not os.path.exists(path):
        raise HTTPException(404, "uploaded image not found")
    return path


async def _build_image_context(image_path: str, user_text: str) -> str:
    try:
        safe_path = _resolve_uploaded_image_path(image_path)
        tool = ToolRegistry.get("analyze_image_tool")
        result = await tool.execute(
            image_path=safe_path,
            user_question=user_text,
            task_type="auto",
            use_ocr=True,
            use_vlm=True,
        )
    except Exception as exc:
        logger.warning("Image context build failed: %s", exc)
        return f"图片解析失败：{exc}"

    if not result.success:
        return f"图片解析失败：{result.error or 'unknown error'}"
    return str(result.data.get("context_for_agent") or "图片解析未得到有效上下文。")


def _format_writing_reply(result: dict) -> str:
    content = result.get("content", "")
    title = result.get("title", "")
    parts: list[str] = []
    if title:
        parts.append(f"## {title}")
    if content:
        parts.append(content)

    citations = result.get("citations") or []
    if citations:
        lines = []
        for c in citations:
            ref = c.get("ref_id") or ""
            name = c.get("title") or c.get("chunk_id") or ""
            page = c.get("page")
            suffix = f", p. {page}" if page else ""
            lines.append(f"- {ref} {name}{suffix}".strip())
        parts.append("### 引用线索\n" + "\n".join(lines))

    usage = result.get("material_usage_summary")
    if usage:
        parts.append("### 素材使用说明\n" + str(usage))

    limitations = result.get("limitations") or []
    if limitations:
        parts.append("### 局限\n" + "\n".join(f"- {x}" for x in limitations))

    next_steps = result.get("suggested_next_steps") or []
    if next_steps:
        parts.append("### 下一步建议\n" + "\n".join(f"- {x}" for x in next_steps))

    return "\n\n".join(parts)


async def _build_response(state, mem: MemoryManager) -> dict:
    intent_out  = state.agent_outputs.get("intent_agent", {})
    user_intent = intent_out.get("result", {}).get("user_intent", "")

    reply = ""
    papers_found: list = []
    papers_downloaded: list = []

    if lib := state.agent_outputs.get("library_agent", {}):
        reply = lib.get("result", {}).get("reply", "")
    elif research := state.agent_outputs.get("research_agent", {}):
        reply = research.get("result", {}).get("reply", "")
    elif general := state.agent_outputs.get("general_agent", {}):
        reply = general.get("result", {}).get("reply", "")
    elif writing := state.agent_outputs.get("writing_agent", {}):
        r = writing.get("result", {})
        reply = _format_writing_reply(r)
    elif web := state.agent_outputs.get("web_agent", {}):
        notes = web.get("result", {}).get("web_notes", [])
        if notes:
            parts = [f"**{n.get('title','')}**\n\n{n.get('answer','')}" for n in notes]
            reply = "\n\n---\n\n".join(parts)
        else:
            reply = web.get("result", {}).get("answer", "")
    elif read := state.agent_outputs.get("reading_agent", {}):
        notes = read.get("result", {}).get("reading_notes", [])
        parts = [f"**{n.get('title','')}**\n\n{n.get('answer','')}" for n in notes]
        reply = "\n\n---\n\n".join(parts)
    elif summ := state.agent_outputs.get("summary_agent", {}):
        reply = summ.get("result", {}).get("final_report", "")
    elif note := state.agent_outputs.get("note_agent", {}):
        reply = note.get("result", {}).get("reply", "")

    lit = state.agent_outputs.get("literature_agent", {})
    if lit:
        r = lit.get("result", {})
        if r.get("reply"):
            reply = r.get("reply", "")
        if user_intent in {"literature_search", "research_literature_reading"}:
            papers_found = r.get("selected_papers", [])
            if papers_found and not reply:
                reply = format_paper_search_reply(papers_found)
        papers_downloaded = lit.get("artifacts", {}).get("downloaded_pdfs", [])
        if papers_downloaded and not reply:
            reply = f"已成功下载 **{len(papers_downloaded)}** 篇论文，现在可以直接提问。"

    if not reply and state.errors:
        reply = f"**遇到问题：** {state.errors[-1]}"

    if state.pending_action and state.pending_action.get("message"):
        msg = state.pending_action.get("message", "")
        reply = (reply + "\n\n" + msg).strip() if reply else msg

    evaluation = None
    try:
        from app.services.evaluation import EvaluationService, build_rag_evaluation_sample

        sample = build_rag_evaluation_sample(state, reply)
        if sample:
            evaluation = await EvaluationService().run(sample)
    except Exception as exc:
        logger.warning("RAG evaluation failed: %s", exc)

    return {
        "reply": reply,
        "intent": user_intent,
        "evaluation": evaluation,
        "papers_found": papers_found,
        "papers_downloaded": papers_downloaded,
        "stored_papers": mem.short_term.stored_papers,
        "pending_action": state.pending_action,
        "compression": mem.short_term.compression_status(),
        "errors": state.errors,
    }


def _update_state(state, user_text: str, response: dict, mem: MemoryManager) -> None:
    sync_memory_from_task_state(
        state=state,
        user_text=user_text,
        assistant_reply=response.get("reply", ""),
        memory_manager=mem,
    )



def _figure_paper_context(doc: dict, max_chars: int = 12000) -> str:
    if not doc:
        return ""
    sections = doc.get("sections") or {}
    if sections:
        priority = [
            "abstract", "introduction", "method", "methods", "methodology",
            "approach", "architecture", "experiments", "results", "discussion",
        ]
        parts: list[str] = []
        used = 0
        seen: set[str] = set()
        for key in priority:
            for name, text in sections.items():
                if name in seen or key not in name.lower():
                    continue
                chunk = f"[{name}]\n{text.strip()}"
                if used + len(chunk) > max_chars:
                    continue
                parts.append(chunk)
                used += len(chunk)
                seen.add(name)
        for name, text in sections.items():
            if name in seen:
                continue
            chunk = f"[{name}]\n{text.strip()}"
            remaining = max_chars - used
            if remaining <= 0:
                break
            parts.append(chunk[:remaining])
            used += min(len(chunk), remaining)
        return "\n\n---\n\n".join(parts)
    return str(doc.get("full_text") or "")[:max_chars]


async def _generate_raster_figure(
    *,
    prompt_text: str,
    negative: str,
    ratio: str,
    provider: str,
    model: str,
    base_url: Optional[str],
) -> tuple[bytes, str, str]:
    from app.config.settings import settings
    from openai import AsyncOpenAI

    provider = (provider or "openai").strip().lower()
    model = (model or "gpt-image-1").strip()
    if provider == "doubao" and "seedream" not in model.lower():
        raise RuntimeError(
            "invalid Doubao image model. Use a Seedream image model such as "
            "doubao-seedream-4.5 or doubao-seedream-4.0, not a Doubao text/chat model."
        )
    api_key = _image_api_key_for_provider(provider, settings)
    if not api_key:
        raise RuntimeError(f"missing API key for image provider '{provider}'")

    resolved_base_url = base_url
    if not resolved_base_url:
        if provider == "doubao":
            resolved_base_url = settings.doubao_base_url
        elif provider == "qwen":
            resolved_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        elif provider == "gemini":
            resolved_base_url = settings.gemini_base_url
        elif provider == "openai":
            resolved_base_url = settings.openai_base_url

    client = AsyncOpenAI(api_key=api_key, base_url=resolved_base_url)
    size = _image_size_for_ratio(ratio)
    prompt = _build_image_prompt(prompt_text, negative, ratio)

    try:
        resp = await client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            response_format="b64_json",
        )
    except TypeError:
        resp = await client.images.generate(model=model, prompt=prompt, size=size)
    except Exception as exc:
        if "response_format" not in str(exc).lower():
            raise
        resp = await client.images.generate(model=model, prompt=prompt, size=size)

    if not resp.data:
        raise RuntimeError("image API returned no image choices")

    item = resp.data[0]
    b64_json = getattr(item, "b64_json", None)
    if b64_json:
        return base64.b64decode(b64_json), "image/png", model

    url = getattr(item, "url", None)
    if url:
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as http:
            image_resp = await http.get(url)
            image_resp.raise_for_status()
            media_type = image_resp.headers.get("content-type", "image/png").split(";", 1)[0]
            return image_resp.content, media_type or "image/png", model

    raise RuntimeError("image API returned neither base64 data nor URL")


def _image_api_key_for_provider(provider: str, settings) -> str:
    if provider == "openai":
        return (settings.openai_api_key or "").strip()
    if provider == "qwen":
        return (settings.qwen_api_key or "").strip()
    if provider == "doubao":
        return (settings.doubao_api_key or settings.ark_api_key or "").strip()
    if provider == "gemini":
        return (settings.gemini_api_key or "").strip()
    return (settings.openai_api_key or "").strip()


def _build_image_prompt(prompt_text: str, negative: str, ratio: str) -> str:
    avoid = negative or "low clarity, garbled text, cluttered layout, text-heavy poster"
    return (
        "Create a polished academic scientific figure as a raster image.\n"
        f"Aspect ratio: {ratio}.\n"
        "Use a clean paper-style layout, coherent visual hierarchy, concise labels, and readable module names.\n"
        f"Avoid: {avoid}.\n\n"
        f"Figure request:\n{prompt_text}"
    )


def _image_size_for_ratio(ratio: str) -> str:
    normalized = (ratio or "").strip()
    if normalized == "1:1":
        return "2048x2048"
    if normalized in {"3:4", "2:3"}:
        return "1728x2304"
    return "2560x1440"


def _clean_svg(raw: str) -> str:
    text = (raw or "").strip()
    if "```" in text:
        blocks = text.split("```")
        for block in blocks:
            candidate = block.strip()
            if candidate.lower().startswith("svg"):
                candidate = candidate[3:].strip()
            if "<svg" in candidate and "</svg>" in candidate:
                text = candidate
                break
    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start < 0 or end < 0:
        return ""
    svg = text[start:end + len("</svg>")]
    svg = re.sub(r"<script\b[^>]*>.*?</script>", "", svg, flags=re.I | re.S)
    svg = re.sub(r"\son[a-zA-Z]+\s*=\s*(['\"]).*?\1", "", svg)
    svg = re.sub(r"\s(href|xlink:href)\s*=\s*(['\"])\s*javascript:.*?\2", "", svg, flags=re.I)
    return svg.strip()
