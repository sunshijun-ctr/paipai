"""Note CRUD exposed as tools.

The note_agent originally bundled all note operations behind a single LLM
agent that dispatched on `task_type`. To make these capabilities available
to a tool-calling agent (ResearchAgent / general_agent), each operation is
also exposed as a standalone BaseTool.

Tools wrap ``app.services.note_service.NoteService`` directly — no LLM
calls inside. LLM-driven note synthesis (create_note_from_summary, etc.)
remains in NoteAgent for now."""
from __future__ import annotations

import logging
from typing import Any

from app.schemas.note_schema import NoteCreate, NoteUpdate
from app.schemas.tool import ToolResult
from app.services.note_service import get_note_service
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _default_user_id(kwargs: dict) -> str:
    return str(kwargs.get("user_id") or "local").strip() or "local"


# ── Create ────────────────────────────────────────────────────────────────

class NoteCreateTool(BaseTool):
    name = "note_create"
    description = "Create a new research note. Body is Markdown."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title":            {"type": "string", "description": "Short note title."},
                "content_markdown": {"type": "string", "description": "Note body in Markdown."},
                "tags":             {"type": "array", "items": {"type": "string"}, "default": []},
                "source_type":      {"type": "string", "default": "manual",
                                     "description": "manual | conversation | reading | summary"},
                "source_id":        {"type": "string", "default": ""},
                "paper_id":         {"type": "string", "default": ""},
                "conversation_id":  {"type": "string", "default": ""},
                "user_id":          {"type": "string", "default": "local"},
            },
            "required": ["title"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        title = str(kwargs.get("title") or "").strip()
        if not title:
            return ToolResult(success=False, error="title is required")
        try:
            note = get_note_service().create_note(NoteCreate(
                user_id=_default_user_id(kwargs),
                title=title,
                content_markdown=str(kwargs.get("content_markdown") or ""),
                source_type=str(kwargs.get("source_type") or "manual"),
                source_id=str(kwargs.get("source_id") or ""),
                paper_id=str(kwargs.get("paper_id") or ""),
                conversation_id=str(kwargs.get("conversation_id") or ""),
                tags=list(kwargs.get("tags") or []),
                metadata=dict(kwargs.get("metadata") or {}),
            ))
        except Exception as exc:
            return ToolResult(success=False, error=f"create_note failed: {exc}")
        return ToolResult(success=True, data={"note": note.model_dump()})


# ── Update ────────────────────────────────────────────────────────────────

class NoteUpdateTool(BaseTool):
    name = "note_update"
    description = "Update an existing note's title, content, or tags."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note_id":          {"type": "string", "description": "ID of the note to update."},
                "title":            {"type": "string"},
                "content_markdown": {"type": "string"},
                "tags":             {"type": "array", "items": {"type": "string"}},
                "source_type":      {"type": "string"},
            },
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        note_id = str(kwargs.get("note_id") or "").strip()
        if not note_id:
            return ToolResult(success=False, error="note_id is required")
        update_fields: dict[str, Any] = {}
        for key in ("title", "content_markdown", "tags", "source_type",
                    "source_id", "paper_id", "conversation_id", "metadata"):
            if key in kwargs and kwargs[key] is not None:
                update_fields[key] = kwargs[key]
        try:
            note = get_note_service().update_note(note_id, NoteUpdate(**update_fields))
        except KeyError:
            return ToolResult(success=False, error=f"note not found: {note_id}")
        except Exception as exc:
            return ToolResult(success=False, error=f"update_note failed: {exc}")
        return ToolResult(success=True, data={"note": note.model_dump()})


# ── Delete ────────────────────────────────────────────────────────────────

class NoteDeleteTool(BaseTool):
    name = "note_delete"
    description = "Delete a note by id. Removes both the row and its vectors."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note_id": {"type": "string"},
            },
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        note_id = str(kwargs.get("note_id") or "").strip()
        if not note_id:
            return ToolResult(success=False, error="note_id is required")
        try:
            ok = get_note_service().delete_note(note_id)
        except Exception as exc:
            return ToolResult(success=False, error=f"delete_note failed: {exc}")
        if not ok:
            return ToolResult(success=False, error=f"note not found: {note_id}")
        return ToolResult(success=True, data={"note_id": note_id, "deleted": True})


# ── List ──────────────────────────────────────────────────────────────────

class NoteListTool(BaseTool):
    name = "note_list"
    description = "List all of the user's notes, newest first. Optional tag / source_type filter."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_id":     {"type": "string", "default": "local"},
                "tag":         {"type": "string", "default": ""},
                "source_type": {"type": "string", "default": ""},
                "limit":       {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        user_id = _default_user_id(kwargs)
        filters: dict[str, Any] = {}
        if kwargs.get("tag"):
            filters["tag"] = str(kwargs["tag"])
        if kwargs.get("source_type"):
            filters["source_type"] = str(kwargs["source_type"])
        limit = int(kwargs.get("limit") or 50)
        try:
            notes = get_note_service().list_notes(user_id=user_id, filters=filters)
        except Exception as exc:
            return ToolResult(success=False, error=f"list_notes failed: {exc}")
        return ToolResult(success=True, data={
            "notes": [n.model_dump() for n in notes[:limit]],
            "total": len(notes),
        })


# ── Search ────────────────────────────────────────────────────────────────

class NoteSearchTool(BaseTool):
    name = "note_search"
    description = (
        "Search the user's notes. Default mode is metadata search (title + tags + "
        "content substring). Set mode='semantic' for vector similarity search "
        "over embedded notes."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query":   {"type": "string"},
                "mode":    {"type": "string", "enum": ["metadata", "semantic"],
                            "default": "metadata"},
                "user_id": {"type": "string", "default": "local"},
                "k":       {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, error="query is required")
        mode = str(kwargs.get("mode") or "metadata").strip().lower()
        user_id = _default_user_id(kwargs)
        svc = get_note_service()
        try:
            if mode == "semantic":
                hits = await svc.search_notes_semantic(
                    query=query, user_id=user_id, k=int(kwargs.get("k") or 5),
                )
                return ToolResult(success=True, data={"mode": "semantic", "hits": hits})
            notes = svc.search_notes_by_metadata(user_id=user_id, query=query)
            return ToolResult(success=True, data={
                "mode": "metadata",
                "notes": [n.model_dump() for n in notes],
            })
        except Exception as exc:
            return ToolResult(success=False, error=f"search_notes failed: {exc}")


# ── Embed ─────────────────────────────────────────────────────────────────

class NoteEmbedTool(BaseTool):
    name = "note_embed"
    description = (
        "Embed (vectorise) a note so it becomes discoverable via semantic "
        "search. Re-embedding the same note updates its vectors."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note_id":  {"type": "string"},
                "reembed":  {"type": "boolean", "default": False,
                             "description": "If true, delete existing vectors first."},
            },
            "required": ["note_id"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        note_id = str(kwargs.get("note_id") or "").strip()
        if not note_id:
            return ToolResult(success=False, error="note_id is required")
        reembed = bool(kwargs.get("reembed", False))
        svc = get_note_service()
        try:
            result = await (svc.reembed_note(note_id) if reembed else svc.embed_note(note_id))
        except KeyError:
            return ToolResult(success=False, error=f"note not found: {note_id}")
        except Exception as exc:
            return ToolResult(success=False, error=f"embed_note failed: {exc}")
        return ToolResult(success=True, data=result)


ALL_NOTE_TOOLS: list[type[BaseTool]] = [
    NoteCreateTool,
    NoteUpdateTool,
    NoteDeleteTool,
    NoteListTool,
    NoteSearchTool,
    NoteEmbedTool,
]
