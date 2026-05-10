"""
Structure-aware PDF indexer.

Strategy:
  1. Use the sections dict from PDF extraction as primary structural boundaries
     (abstract / introduction / method / experiments / conclusion / …).
  2. Within each section, use LangChain RecursiveCharacterTextSplitter to further
     split text that is too long, preserving paragraph breaks where possible.
  3. Each chunk carries metadata: {title, section, chunk_index}

This gives the retriever semantically coherent chunks rather than arbitrary
page-boundary fragments.
"""
import logging
import re
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.storage.base import BaseVectorStore

logger = logging.getLogger(__name__)
_DEFAULT_CHUNK_SIZE = 2000
_DEFAULT_CHUNK_OVERLAP = 200

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=_DEFAULT_CHUNK_SIZE,
    chunk_overlap=_DEFAULT_CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
)

_MIN_SECTION_CHARS = 80   # skip near-empty sections
_SKIP_SECTIONS = {"references", "preamble"}  # low-value for Q&A


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", text.lower())[:48]


def make_splitter(chunk_size: int | None = None, chunk_overlap: int | None = None) -> RecursiveCharacterTextSplitter:
    size = int(chunk_size or _DEFAULT_CHUNK_SIZE)
    overlap = int(chunk_overlap if chunk_overlap is not None else _DEFAULT_CHUNK_OVERLAP)
    if size < 200 or size > 4000:
        raise ValueError("chunk_size must be between 200 and 4000")
    if overlap < 0 or overlap >= size:
        raise ValueError("chunk_overlap must be >= 0 and smaller than chunk_size")
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


async def index_pdf(
    sections: dict[str, str],
    title: str,
    collection: str,
    store: BaseVectorStore,
    rag_chunks: list[dict[str, Any]] | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> int:
    """
    Chunk each PDF section and upsert into the vector store.
    Idempotent: the same (title, section, chunk_index) always produces the same ID.
    Returns the number of chunks upserted.
    """
    slug = _slugify(title)
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    ids: list[str] = []
    splitter = make_splitter(chunk_size, chunk_overlap)
    effective_chunk_size = int(chunk_size or _DEFAULT_CHUNK_SIZE)
    effective_chunk_overlap = int(chunk_overlap if chunk_overlap is not None else _DEFAULT_CHUNK_OVERLAP)

    global_chunk = 0
    source_chunks = rag_chunks or [
        {"text": text, "metadata": {"section": section_name, "chunk_type": "text"}}
        for section_name, text in sections.items()
    ]
    section_counts: dict[str, int] = {}
    for source in source_chunks:
        section_name = str(source.get("metadata", {}).get("section", "body"))
        section_text = str(source.get("text", ""))
        # Skip low-value sections
        if any(section_name.startswith(s) for s in _SKIP_SECTIONS):
            continue
        section_text = section_text.strip()
        if len(section_text) < _MIN_SECTION_CHARS:
            continue

        chunks = splitter.split_text(section_text)
        for chunk_idx, chunk in enumerate(chunks):
            local_idx = section_counts.get(section_name, 0)
            section_counts[section_name] = local_idx + 1
            doc_id = f"{slug}_{section_name}_{local_idx}"
            documents.append(chunk)
            metadata = dict(source.get("metadata", {}))
            metadata.update({
                "title": title,
                "section": section_name,
                "chunk_index": local_idx,
                "global_chunk": global_chunk,
                "chunk_size": effective_chunk_size,
                "chunk_overlap": effective_chunk_overlap,
            })
            metadata.setdefault("chunk_type", "text")
            metadatas.append(metadata)
            ids.append(doc_id)
            global_chunk += 1

    if not documents:
        return 0

    # Write to Chroma (dense) and BM25 (sparse) in parallel
    import asyncio
    from app.rag.temporary import bm25_store

    await asyncio.gather(
        store.add(collection=collection, documents=documents, metadatas=metadatas, ids=ids),
        asyncio.to_thread(bm25_store.add, collection, documents, metadatas, ids),
    )
    logger.info(
        "Indexed '%s': %d chunks across %d sections → Chroma + BM25 ('%s')",
        title, len(documents), len(sections), collection,
    )
    return len(documents)
