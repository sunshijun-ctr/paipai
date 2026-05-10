"""Long-term RAG store — persistent named knowledge-base collections.

Collections (Chroma + BM25):
  lt_docs         — default knowledge base (backward-compatible)
  lib_{slug}      — user-created named knowledge bases
  lt_memory       — indexed memory conclusions (internal; never search with docs)

A JSON registry at {data_dir}/chroma_lt/libraries.json stores library metadata.
All document collections are strictly isolated — searches never cross boundaries
unless explicitly requested.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_LT_MEMORY      = "lt_memory"
_DEFAULT_LIB_ID = "lt_docs"
_DEFAULT_LIB_NAME = "默认知识库"

_store_singleton: "LongTermRAGStore | None" = None


def get_lt_rag_store() -> "LongTermRAGStore":
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = LongTermRAGStore()
    return _store_singleton


def _lib_slug(name: str) -> str:
    """Generate a short, Chroma-safe ID from a library name."""
    ascii_part = re.sub(r"[^a-z0-9]", "_", name.lower())
    ascii_part = re.sub(r"_+", "_", ascii_part).strip("_")
    if len(ascii_part) >= 3:
        return ascii_part[:36]
    return hashlib.md5(name.encode()).hexdigest()[:12]


class LongTermRAGStore:
    """Manages multiple isolated document libraries + lt_memory."""

    def __init__(self) -> None:
        from app.config.settings import settings
        from app.storage.local_chroma import LocalChromaStore

        self._chroma_path    = os.path.join(settings.data_dir, "chroma_lt")
        self._registry_path  = os.path.join(self._chroma_path, "libraries.json")
        self._store          = LocalChromaStore(path=self._chroma_path)
        self._ensure_default()

    async def _add_indexes(
        self,
        lib_id: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> bool:
        """Persist chunks to BM25 and best-effort Chroma.

        A corrupt local Chroma HNSW segment should not prevent uploads from
        being usable through the keyword index or direct session file reading.
        Returns True when the dense Chroma write succeeds.
        """
        from app.rag.temporary import bm25_store

        await asyncio.to_thread(bm25_store.add, lib_id, documents, metadatas, ids)
        try:
            await self._store.add(lib_id, documents, metadatas, ids)
            return True
        except Exception as exc:
            logger.warning(
                "Chroma add failed for '%s'; BM25 index was still written. "
                "Dense retrieval may be unavailable until data/chroma_lt is rebuilt: %s",
                lib_id,
                exc,
            )
            return False

    # ── Registry ──────────────────────────────────────────────────────────────

    def _load_registry(self) -> dict[str, dict]:
        try:
            with open(self._registry_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Library registry load error: %s", exc)
            return {}

    def _save_registry(self, reg: dict) -> None:
        os.makedirs(self._chroma_path, exist_ok=True)
        with open(self._registry_path, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)

    def _ensure_default(self) -> None:
        reg = self._load_registry()
        if _DEFAULT_LIB_ID not in reg:
            reg[_DEFAULT_LIB_ID] = {
                "name": _DEFAULT_LIB_NAME,
                "created_at": datetime.now().isoformat(),
            }
            self._save_registry(reg)

    # ── Library management ────────────────────────────────────────────────────

    def create_library(self, name: str) -> str:
        """Create a new named library. Returns lib_id (idempotent by name)."""
        reg = self._load_registry()
        # Return existing if same name
        for lid, info in reg.items():
            if info.get("name") == name:
                return lid
        slug   = _lib_slug(name)
        lib_id = f"lib_{slug}"
        base, n = lib_id, 2
        while lib_id in reg:
            lib_id = f"{base}_{n}"; n += 1
        reg[lib_id] = {"name": name, "created_at": datetime.now().isoformat()}
        self._save_registry(reg)
        logger.info("Created library '%s' → %s", name, lib_id)
        return lib_id

    async def delete_library(self, lib_id: str) -> None:
        """Delete a library: Chroma collection + BM25 index + registry entry."""
        if lib_id in (_DEFAULT_LIB_ID, _LT_MEMORY):
            raise ValueError(f"Cannot delete reserved collection '{lib_id}'")
        from app.rag.temporary import bm25_store
        reg = self._load_registry()
        reg.pop(lib_id, None)
        self._save_registry(reg)
        await self._store.delete_collection(lib_id)
        try:
            bm25_store._save(lib_id, {})
        except Exception as exc:
            logger.debug("delete_library BM25 cleanup: %s", exc)
        logger.info("Deleted library %s", lib_id)

    def list_libraries(self) -> list[dict]:
        """Return [{lib_id, name, created_at}] — default library first."""
        reg = self._load_registry()
        out = [
            {"lib_id": lid, "name": info.get("name", lid),
             "created_at": info.get("created_at", "")}
            for lid, info in reg.items()
        ]
        out.sort(key=lambda x: (x["lib_id"] != _DEFAULT_LIB_ID, x["created_at"]))
        return out

    def get_library_name(self, lib_id: str) -> str:
        return self._load_registry().get(lib_id, {}).get("name", lib_id)

    # ── Document operations (per library) ─────────────────────────────────────

    async def add_document(
        self,
        local_path: str,
        title: str,
        lib_id: str = _DEFAULT_LIB_ID,
        extra_meta: dict[str, Any] | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> int:
        """Extract, chunk, embed a PDF or text file into *lib_id*.

        Idempotent: same (title, section, chunk_index) → same ID, so re-adding
        the same doc just overwrites existing chunks.
        Returns number of chunks indexed (0 if extraction failed).
        """
        from app.tools.pdf.backends import extract_any
        from app.rag.temporary.indexer import (
            _DEFAULT_CHUNK_OVERLAP,
            _DEFAULT_CHUNK_SIZE,
            _slugify,
            _SKIP_SECTIONS,
            _MIN_SECTION_CHARS,
            make_splitter,
        )
        from app.rag.temporary import bm25_store

        data     = await asyncio.to_thread(extract_any, local_path)
        sections = data.get("sections", {})
        full_text = data.get("full_text", "")
        rag_chunks = data.get("rag_chunks", [])

        # Text files have no sections — treat full_text as one block
        if not sections and full_text:
            sections = {"body": full_text}

        if not sections:
            logger.warning("add_document: no content from '%s'", local_path)
            return 0

        doc_slug  = _slugify(title)
        indexed_at = datetime.now().isoformat()
        documents : list[str]           = []
        metadatas : list[dict[str, Any]] = []
        ids       : list[str]           = []
        splitter = make_splitter(chunk_size, chunk_overlap)
        effective_chunk_size = int(chunk_size or _DEFAULT_CHUNK_SIZE)
        effective_chunk_overlap = int(chunk_overlap if chunk_overlap is not None else _DEFAULT_CHUNK_OVERLAP)

        source_chunks = rag_chunks or [
            {"text": text, "metadata": {"section": sec_name, "chunk_type": "text"}}
            for sec_name, text in sections.items()
        ]
        section_counts: dict[str, int] = {}
        global_chunk = 0

        for source_chunk in source_chunks:
            source_meta = dict(source_chunk.get("metadata", {}))
            sec_name = str(source_meta.get("section", "body"))
            sec_text = str(source_chunk.get("text", ""))
            if any(sec_name.startswith(s) for s in _SKIP_SECTIONS):
                continue
            sec_text = sec_text.strip()
            if len(sec_text) < _MIN_SECTION_CHARS:
                continue
            for chunk in splitter.split_text(sec_text):
                idx = section_counts.get(sec_name, 0)
                section_counts[sec_name] = idx + 1
                doc_id = f"lt_{doc_slug}_{sec_name}_{idx}"
                meta: dict[str, Any] = {
                    "title": title, "section": sec_name,
                    "source": local_path, "lib_id": lib_id,
                    "indexed_at": indexed_at,
                    "chunk_index": idx,
                    "global_chunk": global_chunk,
                    "chunk_size": effective_chunk_size,
                    "chunk_overlap": effective_chunk_overlap,
                }
                meta.update(source_meta)
                meta.update({"title": title, "section": sec_name, "source": local_path, "lib_id": lib_id})
                if extra_meta:
                    meta.update(extra_meta)
                documents.append(chunk)
                metadatas.append(meta)
                ids.append(doc_id)
                global_chunk += 1

        if not documents:
            return 0

        await self._add_indexes(lib_id, documents, metadatas, ids)
        logger.info("add_document: '%s' → %d chunks in '%s'", title, len(documents), lib_id)
        return len(documents)

    async def remove_document(self, title: str, lib_id: str = _DEFAULT_LIB_ID) -> None:
        """Remove all chunks for *title* from *lib_id*."""
        from app.rag.temporary import bm25_store
        try:
            col = self._store.get_raw_collection(lib_id)
            await asyncio.to_thread(col.delete, where={"title": title})
            logger.info("remove_document: '%s' from '%s' (Chroma)", title, lib_id)
        except Exception as exc:
            logger.warning("remove_document Chroma %s: %s", lib_id, exc)
        try:
            docs     = bm25_store._load(lib_id)
            filtered = {k: v for k, v in docs.items()
                        if v.get("metadata", {}).get("title") != title}
            bm25_store._save(lib_id, filtered)
        except Exception as exc:
            logger.warning("remove_document BM25 %s: %s", lib_id, exc)

    async def add_text_chunks(
        self,
        *,
        title: str,
        chunks: list[str],
        lib_id: str = _DEFAULT_LIB_ID,
        source: str = "",
        extra_meta: dict[str, Any] | None = None,
    ) -> int:
        """Index already-split text chunks into a knowledge-base library."""
        from app.rag.temporary import bm25_store
        from app.rag.temporary.indexer import _slugify

        clean_chunks = [chunk.strip() for chunk in chunks if str(chunk).strip()]
        if not clean_chunks:
            return 0

        doc_slug = _slugify(title)
        source_hash = hashlib.md5((source or title).encode()).hexdigest()[:10]
        indexed_at = datetime.now().isoformat()
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        ids: list[str] = []

        for idx, chunk in enumerate(clean_chunks):
            meta: dict[str, Any] = {
                "title": title,
                "section": "note",
                "source": source,
                "lib_id": lib_id,
                "indexed_at": indexed_at,
                "chunk_index": idx,
                "global_chunk": idx,
                "chunk_type": "text",
            }
            if extra_meta:
                meta.update(extra_meta)
            documents.append(chunk)
            metadatas.append(meta)
            ids.append(f"lt_{doc_slug}_{source_hash}_{idx:03d}")

        await self._add_indexes(lib_id, documents, metadatas, ids)
        logger.info("add_text_chunks: '%s' -> %d chunks in '%s'", title, len(documents), lib_id)
        return len(documents)

    async def remove_document_source(self, source: str, lib_id: str = _DEFAULT_LIB_ID) -> None:
        """Remove all chunks with a specific source marker from a library."""
        if not source:
            return
        from app.rag.temporary import bm25_store

        try:
            col = self._store.get_raw_collection(lib_id)
            await asyncio.to_thread(col.delete, where={"source": source})
        except Exception as exc:
            logger.warning("remove_document_source Chroma %s/%s: %s", lib_id, source, exc)

        try:
            docs = bm25_store._load(lib_id)
            filtered = {
                key: value for key, value in docs.items()
                if value.get("metadata", {}).get("source") != source
            }
            bm25_store._save(lib_id, filtered)
        except Exception as exc:
            logger.warning("remove_document_source BM25 %s/%s: %s", lib_id, source, exc)

    async def list_documents(self, lib_id: str = _DEFAULT_LIB_ID) -> list[str]:
        """Return deduplicated document titles in *lib_id*."""
        try:
            col  = self._store.get_raw_collection(lib_id)
            res  = await asyncio.to_thread(col.get, include=["metadatas"])
            seen : set[str]  = set()
            out  : list[str] = []
            for meta in res.get("metadatas") or []:
                t = (meta or {}).get("title", "")
                if t and t not in seen:
                    seen.add(t); out.append(t)
            return out
        except Exception as exc:
            logger.debug("list_documents %s: %s", lib_id, exc)
            return []

    async def list_document_records(self, lib_id: str = _DEFAULT_LIB_ID) -> list[dict[str, Any]]:
        """Return deduplicated document records with source-file metadata."""
        def _records_from_metadatas(metadatas: list[dict[str, Any]]) -> list[dict[str, Any]]:
            grouped: dict[tuple[str, str], dict[str, Any]] = {}
            for meta in metadatas:
                meta = meta or {}
                title = meta.get("title", "")
                source = meta.get("source", "")
                if not title:
                    continue
                key = (title, source)
                rec = grouped.setdefault(
                    key,
                    {
                        "title": title,
                        "source": source,
                        "lib_id": meta.get("lib_id", lib_id),
                        "indexed_at": meta.get("indexed_at", ""),
                        "source_type": meta.get("source_type", ""),
                        "note_id": meta.get("note_id", ""),
                        "venue": meta.get("venue", ""),
                        "journal": meta.get("journal", ""),
                        "doi": meta.get("doi", ""),
                        "paper_source": meta.get("paper_source", ""),
                        "published_date": meta.get("published_date", ""),
                        "sections": set(),
                        "chunk_count": 0,
                    },
                )
                for field in ("venue", "journal", "doi", "paper_source", "published_date", "source_type", "note_id"):
                    if meta.get(field) and not rec.get(field):
                        rec[field] = meta.get(field)
                if meta.get("indexed_at"):
                    current = rec.get("indexed_at", "")
                    rec["indexed_at"] = max(current, meta["indexed_at"]) if current else meta["indexed_at"]
                if meta.get("section"):
                    rec["sections"].add(meta["section"])
                rec["chunk_count"] += 1

            out: list[dict[str, Any]] = []
            for rec in grouped.values():
                source = rec.get("source", "")
                rec["sections"] = sorted(rec["sections"])
                rec["file_exists"] = source.startswith("note://") or bool(source and os.path.exists(source))
                rec["file_ext"] = os.path.splitext(source)[1].lower() if source else ""
                if not rec.get("indexed_at") and rec["file_exists"] and not source.startswith("note://"):
                    rec["indexed_at"] = datetime.fromtimestamp(os.path.getmtime(source)).isoformat()
                out.append(rec)
            out.sort(key=lambda x: x.get("indexed_at", ""), reverse=True)
            return out

        try:
            col = self._store.get_raw_collection(lib_id)
            res = await asyncio.to_thread(col.get, include=["metadatas"])
            return _records_from_metadatas(res.get("metadatas") or [])
        except Exception as exc:
            logger.debug("list_document_records Chroma %s: %s", lib_id, exc)
            try:
                from app.rag.temporary import bm25_store
                docs = bm25_store._load(lib_id)
                return _records_from_metadatas([
                    value.get("metadata", {}) for value in docs.values()
                ])
            except Exception as bm25_exc:
                logger.debug("list_document_records BM25 %s: %s", lib_id, bm25_exc)
                return []

    async def list_document_chunks(
        self,
        title: str,
        lib_id: str = _DEFAULT_LIB_ID,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return stored chunks for one document exactly as indexed in Chroma."""
        try:
            col = self._store.get_raw_collection(lib_id)
            res = await asyncio.to_thread(
                col.get,
                where={"title": title},
                include=["documents", "metadatas"],
                limit=limit,
            )
            ids = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            chunks: list[dict[str, Any]] = []
            for idx, (chunk_id, document, meta) in enumerate(zip(ids, docs, metas)):
                meta = meta or {}
                chunks.append({
                    "id": chunk_id,
                    "text": document,
                    "metadata": meta,
                    "section": meta.get("section", ""),
                    "chunk_type": meta.get("chunk_type", "text"),
                    "page": meta.get("page", 0),
                    "chunk_index": meta.get("chunk_index", idx),
                    "global_chunk": meta.get("global_chunk", idx),
                    "length": len(document or ""),
                })
            chunks.sort(key=lambda c: (
                int(c.get("global_chunk") or 0),
                str(c.get("section") or ""),
                int(c.get("chunk_index") or 0),
            ))
            return chunks
        except Exception as exc:
            logger.debug("list_document_chunks Chroma %s/%s: %s", lib_id, title, exc)
            try:
                from app.rag.temporary import bm25_store
                docs = bm25_store._load(lib_id)
                chunks: list[dict[str, Any]] = []
                for idx, (chunk_id, item) in enumerate(docs.items()):
                    meta = item.get("metadata", {}) or {}
                    if meta.get("title") != title:
                        continue
                    document = item.get("document", "")
                    chunks.append({
                        "id": chunk_id,
                        "text": document,
                        "metadata": meta,
                        "section": meta.get("section", ""),
                        "chunk_type": meta.get("chunk_type", "text"),
                        "page": meta.get("page", 0),
                        "chunk_index": meta.get("chunk_index", idx),
                        "global_chunk": meta.get("global_chunk", idx),
                        "length": len(document or ""),
                    })
                chunks.sort(key=lambda c: (
                    int(c.get("global_chunk") or 0),
                    str(c.get("section") or ""),
                    int(c.get("chunk_index") or 0),
                ))
                return chunks[:limit]
            except Exception as bm25_exc:
                logger.debug("list_document_chunks BM25 %s/%s: %s", lib_id, title, bm25_exc)
                return []

    async def list_all_documents(self) -> dict[str, list[str]]:
        """Return {lib_id: [titles]} for all registered libraries."""
        result: dict[str, list[str]] = {}
        for lib in self.list_libraries():
            result[lib["lib_id"]] = await self.list_documents(lib["lib_id"])
        return result

    async def search_documents(
        self,
        query: str,
        lib_ids: list[str] | None = None,
        k: int = 8,
        title_filter: str = "",
    ) -> list[dict[str, Any]]:
        """Hybrid search across the specified libraries (all if lib_ids is None).

        Results from each library are tagged with the library name so the LLM
        can cite the source domain.
        """
        from app.rag.temporary.retriever import retrieve

        if lib_ids is None:
            lib_ids = [lib["lib_id"] for lib in self.list_libraries()]

        if not lib_ids:
            return []

        k_per = max(k // len(lib_ids), 3)
        all_chunks: list[dict[str, Any]] = []

        for lid in lib_ids:
            try:
                chunks = await retrieve(
                    query=query, collection=lid,
                    store=self._store, top_n=k_per,
                    title_filter=title_filter,
                )
                lib_name = self.get_library_name(lid)
                for c in chunks:
                    c.setdefault("metadata", {})["lib_name"] = lib_name
                all_chunks.extend(chunks)
            except Exception as exc:
                logger.debug("search_documents %s: %s", lid, exc)

        # Re-rank by RRF score, return top-k
        all_chunks.sort(key=lambda x: x.get("rrf_score", 0), reverse=True)
        return all_chunks[:k]

    # ── Memory conclusion index (lt_memory) — unchanged ───────────────────────

    async def index_conclusion(self, content: str, topic: str, session_id: str) -> None:
        from app.rag.temporary import bm25_store
        doc_id = f"mem_{hashlib.md5(content.encode()).hexdigest()[:16]}"
        meta: dict[str, Any] = {"topic": topic, "session_id": session_id}
        await asyncio.gather(
            self._store.add(_LT_MEMORY, [content], [meta], [doc_id]),
            asyncio.to_thread(bm25_store.add, _LT_MEMORY, [content], [meta], [doc_id]),
        )
        logger.debug("index_conclusion: topic='%s' id=%s", topic, doc_id)

    async def search_memory(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        from app.rag.temporary.retriever import retrieve
        return await retrieve(query=query, collection=_LT_MEMORY,
                              store=self._store, top_n=k)
