import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config.settings import settings
from app.schemas.note_schema import Note, NoteCreate, NoteUpdate
from app.storage.local_chroma import LocalChromaStore

logger = logging.getLogger(__name__)

_NOTES_PATH = os.path.join(".", "data", "notes", "notes.json")
_NOTE_COLLECTION = "notes"

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=800,
    chunk_overlap=120,
    separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " ", ""],
)

_service_singleton: Optional["NoteService"] = None


def get_note_service() -> "NoteService":
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = NoteService()
    return _service_singleton


class NoteService:
    """Note service with PostgreSQL primary storage and JSON fallback."""

    def __init__(self, path: str = _NOTES_PATH) -> None:
        self._path = path
        self._store = LocalChromaStore(path=os.path.join(settings.data_dir, "chroma_lt"))
        self._notes: dict[str, Note] = {}
        self._repo = None
        if settings.database_url:
            try:
                from app.storage.postgres.note_repository import PostgresNoteRepository
                self._repo = PostgresNoteRepository(settings.database_url)
                self._migrate_json_to_postgres()
                logger.info("NoteService using PostgreSQL storage")
            except Exception as exc:
                logger.warning("PostgreSQL note storage unavailable, falling back to JSON: %s", exc)
                self._repo = None
        if self._repo is None:
            self._load()
            logger.info("NoteService using JSON storage: %s", self._path)

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            self._notes = {item["id"]: Note(**item) for item in raw}
        except FileNotFoundError:
            self._notes = {}
        except Exception:
            self._notes = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        data = [note.model_dump() for note in self._notes.values()]
        data.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def create_note(self, payload: NoteCreate) -> Note:
        note = Note(id=f"note_{uuid.uuid4().hex[:12]}", **payload.model_dump())
        if self._repo:
            return self._repo.create(note)
        self._notes[note.id] = note
        self._save()
        return note

    def update_note(self, note_id: str, payload: NoteUpdate) -> Note:
        if self._repo:
            return self._repo.update(note_id, payload)
        note = self.get_note(note_id)
        data = payload.model_dump(exclude_unset=True)
        content_changed = "content_markdown" in data and data["content_markdown"] != note.content_markdown
        for key, value in data.items():
            if value is not None:
                setattr(note, key, value)
        if content_changed and note.embedding_status == "embedded":
            note.embedding_status = "outdated"
        note.updated_at = datetime.now().isoformat()
        self._notes[note.id] = note
        self._save()
        return note

    def delete_note(self, note_id: str) -> bool:
        if self._repo:
            deleted = self._repo.delete(note_id)
            if not deleted:
                return False
        else:
            if note_id not in self._notes:
                return False
            self._notes.pop(note_id)
            self._save()
        try:
            col = self._store.get_raw_collection(_NOTE_COLLECTION)
            col.delete(where={"note_id": note_id})
        except Exception:
            pass
        return True

    def get_note(self, note_id: str) -> Note:
        if self._repo:
            return self._repo.get(note_id)
        if note_id not in self._notes:
            raise KeyError(f"Note not found: {note_id}")
        return self._notes[note_id]

    def list_notes(self, user_id: str = "local", filters: Optional[dict[str, Any]] = None) -> list[Note]:
        if self._repo:
            return self._repo.list(user_id=user_id, filters=filters)
        filters = filters or {}
        query = (filters.get("query") or "").strip().lower()
        tag = (filters.get("tag") or "").strip().lower()
        source_type = (filters.get("source_type") or "").strip()
        notes = [n for n in self._notes.values() if n.user_id == user_id]
        if query:
            notes = [
                n for n in notes
                if query in n.title.lower()
                or query in n.content_markdown.lower()
                or any(query in t.lower() for t in n.tags)
            ]
        if tag:
            notes = [n for n in notes if any(tag == t.lower() for t in n.tags)]
        if source_type:
            notes = [n for n in notes if n.source_type == source_type]
        notes.sort(key=lambda n: n.updated_at, reverse=True)
        return notes

    def search_notes_by_metadata(self, user_id: str, query: str, filters: Optional[dict[str, Any]] = None) -> list[Note]:
        if self._repo:
            return self._repo.search_metadata(user_id=user_id, query=query, filters=filters)
        filters = dict(filters or {})
        filters["query"] = query
        return self.list_notes(user_id=user_id, filters=filters)

    async def embed_note(self, note_id: str) -> dict[str, Any]:
        note = self.get_note(note_id)
        await self._delete_note_vectors(note_id)
        chunks = [c.strip() for c in _splitter.split_text(note.content_markdown) if c.strip()]
        if not chunks:
            return {"note_id": note_id, "chunks_indexed": 0, "embedding_status": note.embedding_status}

        metadatas = [
            {
                "note_id": note.id,
                "title": note.title,
                "user_id": note.user_id,
                "paper_id": note.paper_id,
                "tags": ", ".join(note.tags),
                "source_type": note.source_type,
                "created_at": note.created_at,
                "chunk_index": idx,
            }
            for idx, _ in enumerate(chunks)
        ]
        ids = [f"{note.id}_chunk_{idx:03d}" for idx in range(len(chunks))]
        from app.rag.long_term.store import get_lt_rag_store

        library_chunks_indexed = await get_lt_rag_store().add_text_chunks(
            title=note.title,
            chunks=chunks,
            source=_note_source(note.id),
            extra_meta={
                "source_type": "note",
                "note_id": note.id,
                "tags": ", ".join(note.tags),
            },
        )
        await self._store.add(_NOTE_COLLECTION, chunks, metadatas, ids)
        note.embedding_status = "embedded"
        note.updated_at = datetime.now().isoformat()
        if self._repo:
            self._repo.create(note)
        else:
            self._notes[note.id] = note
            self._save()
        return {
            "note_id": note_id,
            "chunks_indexed": len(chunks),
            "library_chunks_indexed": library_chunks_indexed,
            "embedding_status": note.embedding_status,
        }

    async def reembed_note(self, note_id: str) -> dict[str, Any]:
        return await self.embed_note(note_id)

    async def unembed_note(self, note_id: str) -> dict[str, Any]:
        note = self.get_note(note_id)
        await self._delete_note_vectors(note_id)
        note.embedding_status = "not_embedded"
        note.updated_at = datetime.now().isoformat()
        if self._repo:
            self._repo.update(note_id, NoteUpdate(embedding_status=note.embedding_status))
        else:
            self._notes[note.id] = note
            self._save()
        return {"note_id": note_id, "embedding_status": note.embedding_status}

    async def sync_embedded_notes_to_library(self, user_id: str = "local") -> dict[str, Any]:
        synced = 0
        chunks_indexed = 0
        notes = [note for note in self.list_notes(user_id) if note.embedding_status == "embedded"]
        for note in notes:
            chunks = [c.strip() for c in _splitter.split_text(note.content_markdown) if c.strip()]
            if not chunks:
                continue
            from app.rag.long_term.store import get_lt_rag_store
            count = await get_lt_rag_store().add_text_chunks(
                title=note.title,
                chunks=chunks,
                source=_note_source(note.id),
                extra_meta={
                    "source_type": "note",
                    "note_id": note.id,
                    "tags": ", ".join(note.tags),
                },
            )
            synced += 1
            chunks_indexed += count
        return {"notes_synced": synced, "chunks_indexed": chunks_indexed}

    async def search_notes_semantic(self, query: str, user_id: str = "local", k: int = 5) -> list[dict[str, Any]]:
        hits = await self._store.query(_NOTE_COLLECTION, [query], n_results=k, where={"user_id": user_id})
        out: list[dict[str, Any]] = []
        for hit in hits:
            note_id = hit.get("metadata", {}).get("note_id", "")
            try:
                note = self.get_note(note_id)
            except KeyError:
                continue
            out.append({"note": note.model_dump(), "chunk": hit.get("document", ""), "metadata": hit.get("metadata", {})})
        return out

    async def _delete_note_vectors(self, note_id: str) -> None:
        try:
            col = self._store.get_raw_collection(_NOTE_COLLECTION)
            await asyncio.to_thread(col.delete, where={"note_id": note_id})
        except Exception:
            pass
        try:
            from app.rag.long_term.store import get_lt_rag_store
            await get_lt_rag_store().remove_document_source(_note_source(note_id))
        except Exception as exc:
            logger.warning("Failed to remove note from knowledge base: %s", exc)

    def _migrate_json_to_postgres(self) -> None:
        if not self._repo or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            migrated = 0
            for item in raw:
                note = Note(**item)
                try:
                    self._repo.get(note.id)
                except KeyError:
                    self._repo.create(note)
                    migrated += 1
            if migrated:
                logger.info("Migrated %d JSON note(s) to PostgreSQL", migrated)
        except Exception as exc:
            logger.warning("JSON note migration skipped: %s", exc)


def _note_source(note_id: str) -> str:
    return f"note://{note_id}"
