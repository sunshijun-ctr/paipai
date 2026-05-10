from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from app.schemas.note_schema import Note, NoteCreate, NoteUpdate


class PostgresNoteRepository:
    def __init__(self, database_url: str) -> None:
        import psycopg

        self._database_url = database_url
        self._psycopg = psycopg
        self.init_schema()

    def _connect(self):
        return self._psycopg.connect(self._database_url)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    title TEXT NOT NULL,
                    content_markdown TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    source_id TEXT DEFAULT '',
                    paper_id TEXT DEFAULT '',
                    conversation_id TEXT DEFAULT '',
                    tags TEXT[] DEFAULT '{}',
                    embedding_status TEXT NOT NULL DEFAULT 'not_embedded',
                    metadata JSONB DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_user_updated ON notes(user_id, updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_tags ON notes USING GIN(tags)")

    def create(self, note: Note) -> Note:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (
                    id, user_id, title, content_markdown, source_type, source_id,
                    paper_id, conversation_id, tags, embedding_status, metadata,
                    created_at, updated_at
                )
                VALUES (
                    %(id)s, %(user_id)s, %(title)s, %(content_markdown)s, %(source_type)s, %(source_id)s,
                    %(paper_id)s, %(conversation_id)s, %(tags)s, %(embedding_status)s, %(metadata)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content_markdown = EXCLUDED.content_markdown,
                    source_type = EXCLUDED.source_type,
                    source_id = EXCLUDED.source_id,
                    paper_id = EXCLUDED.paper_id,
                    conversation_id = EXCLUDED.conversation_id,
                    tags = EXCLUDED.tags,
                    embedding_status = EXCLUDED.embedding_status,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                self._params(note),
            )
        return note

    def update(self, note_id: str, payload: NoteUpdate) -> Note:
        note = self.get(note_id)
        data = payload.model_dump(exclude_unset=True)
        content_changed = "content_markdown" in data and data["content_markdown"] != note.content_markdown
        for key, value in data.items():
            if value is not None:
                setattr(note, key, value)
        if content_changed and note.embedding_status == "embedded":
            note.embedding_status = "outdated"
        note.updated_at = datetime.now().isoformat()
        return self.create(note)

    def delete(self, note_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM notes WHERE id = %s", (note_id,))
            return cur.rowcount > 0

    def get(self, note_id: str) -> Note:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, title, content_markdown, source_type, source_id,
                       paper_id, conversation_id, tags, embedding_status, metadata,
                       created_at, updated_at
                FROM notes WHERE id = %s
                """,
                (note_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Note not found: {note_id}")
        return self._row_to_note(row)

    def list(self, user_id: str = "local", filters: Optional[dict[str, Any]] = None) -> list[Note]:
        filters = filters or {}
        query = (filters.get("query") or "").strip()
        tag = (filters.get("tag") or "").strip()
        source_type = (filters.get("source_type") or "").strip()

        where = ["user_id = %(user_id)s"]
        params: dict[str, Any] = {"user_id": user_id}
        if query:
            where.append("(title ILIKE %(query)s OR content_markdown ILIKE %(query)s OR %(query_plain)s = ANY(tags))")
            params["query"] = f"%{query}%"
            params["query_plain"] = query
        if tag:
            where.append("%(tag)s = ANY(tags)")
            params["tag"] = tag
        if source_type:
            where.append("source_type = %(source_type)s")
            params["source_type"] = source_type

        sql = f"""
            SELECT id, user_id, title, content_markdown, source_type, source_id,
                   paper_id, conversation_id, tags, embedding_status, metadata,
                   created_at, updated_at
            FROM notes
            WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_note(r) for r in rows]

    def search_metadata(self, user_id: str, query: str, filters: Optional[dict[str, Any]] = None) -> list[Note]:
        filters = dict(filters or {})
        filters["query"] = query
        return self.list(user_id=user_id, filters=filters)

    def _params(self, note: Note) -> dict[str, Any]:
        return {
            **note.model_dump(exclude={"metadata"}),
            "metadata": json.dumps(note.metadata, ensure_ascii=False),
        }

    def _row_to_note(self, row) -> Note:
        metadata = row[10] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return Note(
            id=row[0],
            user_id=row[1],
            title=row[2],
            content_markdown=row[3],
            source_type=row[4] or "manual",
            source_id=row[5] or "",
            paper_id=row[6] or "",
            conversation_id=row[7] or "",
            tags=list(row[8] or []),
            embedding_status=row[9] or "not_embedded",
            metadata=metadata,
            created_at=row[11].isoformat() if hasattr(row[11], "isoformat") else str(row[11]),
            updated_at=row[12].isoformat() if hasattr(row[12], "isoformat") else str(row[12]),
        )
