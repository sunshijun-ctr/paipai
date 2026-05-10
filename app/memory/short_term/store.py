"""Short-term memory store — maintains current session continuity with three-part structure:
recent turns (raw) + history summary (compressed) + current focus.
"""
import json
import logging
import os
from datetime import datetime
from typing import Any

from app.session.context import SessionContext

logger = logging.getLogger(__name__)

_SESSIONS_DIR = os.path.join(".", "data", "memory", "sessions")
_MAX_RECENT_TURNS = 100  # messages (= 50 full exchanges)
_COMPRESSION_WARN_TURNS = 90


class ShortTermMemoryStore:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._path = os.path.join(_SESSIONS_DIR, f"{session_id}.json")
        self._data: dict[str, Any] = {
            "session_id": session_id,
            "recent_turns": [],       # last N raw conversation messages
            "history_summary": "",    # compressed summary of older turns
            "current_task": "",       # latest unfinished or follow-up user intent
            "current_focus": "",      # what is currently being discussed
            "active_task_context": {}, # structured current task workspace
            "current_library_context": {},  # last library QA retrieval result for follow-up questions
            "current_writing_context": {},  # last writing output for follow-up editing/expansion
            "stored_papers": [],      # downloaded papers (persisted for restart)
            "found_papers": [],       # search results (persisted for restart)
            "pending_action": None,   # workflow confirmation waiting for the next user turn
            "session_context": {},
            "updated_at": "",
        }
        self.load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                loaded = json.load(f)
            self._data.update(loaded)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Failed to load short-term memory [%s]: %s", self._session_id, exc)

    def save(self) -> None:
        try:
            os.makedirs(_SESSIONS_DIR, exist_ok=True)
            self._sync_session_context_data()
            self._data["updated_at"] = datetime.now().isoformat()
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(_json_safe(self._data), f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("Failed to save short-term memory: %s", exc)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def recent_turns(self) -> list[dict]:
        return self._data.get("recent_turns", [])

    @property
    def history_summary(self) -> str:
        summary = self._data.get("history_summary", "")
        if isinstance(summary, dict):
            return str(summary.get("summary") or "")
        return str(summary or "")

    @history_summary.setter
    def history_summary(self, value: str) -> None:
        self._data["history_summary"] = value

    @property
    def current_task(self) -> str:
        return str(self._data.get("current_task") or "")

    @current_task.setter
    def current_task(self, value: str) -> None:
        self._data["current_task"] = value

    @property
    def current_focus(self) -> str:
        return self._data.get("current_focus", "")

    @current_focus.setter
    def current_focus(self, value: str) -> None:
        self._data["current_focus"] = value

    @property
    def active_task_context(self) -> dict[str, Any]:
        return self._data.get("active_task_context", {})

    @active_task_context.setter
    def active_task_context(self, value: dict[str, Any]) -> None:
        self._data["active_task_context"] = value

    @property
    def current_library_context(self) -> dict[str, Any]:
        return self._data.get("current_library_context", {})

    @current_library_context.setter
    def current_library_context(self, value: dict[str, Any]) -> None:
        self._data["current_library_context"] = value

    @property
    def current_writing_context(self) -> dict[str, Any]:
        return self._data.get("current_writing_context", {})

    @current_writing_context.setter
    def current_writing_context(self, value: dict[str, Any]) -> None:
        self._data["current_writing_context"] = value

    @property
    def stored_papers(self) -> list:
        return self._data.get("stored_papers", [])

    @stored_papers.setter
    def stored_papers(self, value: list) -> None:
        self._data["stored_papers"] = value

    @property
    def found_papers(self) -> list:
        return self._data.get("found_papers", [])

    @found_papers.setter
    def found_papers(self, value: list) -> None:
        self._data["found_papers"] = value

    @property
    def pending_action(self) -> dict[str, Any] | None:
        return self._data.get("pending_action")

    @pending_action.setter
    def pending_action(self, value: dict[str, Any] | None) -> None:
        self._data["pending_action"] = value

    @property
    def session_context(self) -> SessionContext:
        raw = self._data.get("session_context") or {}
        ctx = SessionContext.from_dict(raw, session_id=self._session_id)
        ctx.recent_turns = list(self._data.get("recent_turns", []))
        ctx.current_task = self.current_task or ctx.current_task
        ctx.history_summary = self.history_summary or ctx.history_summary
        return ctx

    @session_context.setter
    def session_context(self, value: SessionContext | dict[str, Any]) -> None:
        ctx = value if isinstance(value, SessionContext) else SessionContext.from_dict(value, self._session_id)
        ctx.session_id = ctx.session_id or self._session_id
        ctx.recent_turns = list(self._data.get("recent_turns", []))
        ctx.current_task = ctx.current_task or self.current_task
        ctx.history_summary = ctx.history_summary or self.history_summary
        self._data["session_context"] = ctx.to_dict()
        self._data["current_task"] = ctx.current_task
        self._data["history_summary"] = ctx.history_summary

    # ── Mutation ──────────────────────────────────────────────────────────────

    def add_turn(self, user_msg: str, assistant_msg: str) -> None:
        turns = self._data.setdefault("recent_turns", [])
        turns.append({"role": "user", "content": user_msg})
        turns.append({"role": "assistant", "content": assistant_msg})
        self._sync_session_context_data()

    def needs_compression(self) -> bool:
        return len(self._data.get("recent_turns", [])) > _MAX_RECENT_TURNS

    def nearing_compression_limit(self) -> bool:
        n = len(self._data.get("recent_turns", []))
        return _COMPRESSION_WARN_TURNS <= n <= _MAX_RECENT_TURNS

    def compression_status(self) -> dict[str, Any]:
        n = len(self._data.get("recent_turns", []))
        return {
            "recent_count": n,
            "max_recent": _MAX_RECENT_TURNS,
            "warn_at": _COMPRESSION_WARN_TURNS,
            "needs_compression": n > _MAX_RECENT_TURNS,
            "near_limit": _COMPRESSION_WARN_TURNS <= n <= _MAX_RECENT_TURNS,
            "has_summary": bool(self._data.get("history_summary")),
        }

    def compress_old_turns(self, llm_summary: str = "") -> None:
        """Move oldest turns into history_summary to keep recent_turns bounded.
        Caller should pass an LLM-generated JSON summary; falls back to simple text concat.
        """
        turns = self._data.get("recent_turns", [])
        if len(turns) <= _MAX_RECENT_TURNS:
            return

        overflow = turns[: len(turns) - _MAX_RECENT_TURNS]
        self._data["recent_turns"] = turns[len(turns) - _MAX_RECENT_TURNS :]

        if llm_summary:
            self._apply_llm_history_summary(llm_summary)
        else:
            lines = [f"{m['role']}: {m['content'][:300]}" for m in overflow]
            existing = self.history_summary
            appended = "\n".join(lines)
            self._data["history_summary"] = (existing + "\n" + appended).strip()
        self._sync_session_context_data()

    # ── Context for agents ────────────────────────────────────────────────────

    def get_full_history(self) -> list[dict]:
        """Return recent_turns as conversation history for agent injection."""
        return list(self._data.get("recent_turns", []))

    def to_context_string(self) -> str:
        parts: list[str] = []
        if self.current_task:
            parts.append(f"Current user intent: {self.current_task}")
        if self.history_summary:
            parts.append(f"Earlier in this session: {self.history_summary}")
        if self._data.get("current_focus"):
            parts.append(f"Current focus: {self._data['current_focus']}")
        task_ctx = self._data.get("active_task_context") or {}
        if task_ctx.get("status") == "active" and task_ctx.get("subject_content"):
            parts.append(
                "Active task: "
                f"{task_ctx.get('task_type', '')} / {task_ctx.get('subject_title', '')}\n"
                f"Subject excerpt: {str(task_ctx.get('subject_content', ''))[:800]}"
            )
        return "\n".join(parts)

    def _apply_llm_history_summary(self, llm_summary: str) -> None:
        try:
            parsed = json.loads(_strip_json_code_fence(llm_summary))
        except Exception:
            self._data["history_summary"] = llm_summary
            return

        if not isinstance(parsed, dict):
            self._data["history_summary"] = llm_summary
            return

        summary = str(parsed.get("summary") or "").strip()
        current_task = str(parsed.get("current_task") or "").strip()
        if summary:
            self._data["history_summary"] = summary
        if current_task:
            self._data["current_task"] = current_task
        self._sync_session_context_data()

    def _sync_session_context_data(self) -> None:
        raw = self._data.setdefault("session_context", {})
        if not isinstance(raw, dict):
            raw = {}
            self._data["session_context"] = raw
        raw["session_id"] = self._session_id
        raw["recent_turns"] = list(self._data.get("recent_turns", []))
        raw["current_task"] = self.current_task
        raw["history_summary"] = self.history_summary


def _json_safe(value: Any) -> Any:
    """Convert numpy/scalar-like values from retrieval metadata into JSON-safe data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "tolist"):
        try:
            return _json_safe(value.tolist())
        except Exception:
            pass
    return str(value)


def _strip_json_code_fence(text: str) -> str:
    content = text.strip()
    if content.startswith("```json"):
        content = content.split("```json", 1)[1].split("```", 1)[0]
    elif content.startswith("```"):
        content = content.split("```", 1)[1].split("```", 1)[0]
    return content.strip()
