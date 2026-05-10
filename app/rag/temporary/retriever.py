"""
Hybrid retrieval pipeline: BM25 (sparse) + Chroma (dense) → RRF merge → rerank.

Flow:
  1. Run BM25 keyword search     → up to fetch_k candidates  (exact keyword match)
  2. Run Chroma bi-encoder search → up to fetch_k candidates  (semantic similarity)
  3. Reciprocal Rank Fusion (RRF) to merge both ranked lists
  4. Reranker cross-scores the merged candidates → return top_n

RRF formula: score(d) = Σ 1 / (k + rank_i(d))   where k=60 (standard default)
"""
import asyncio
import logging
from typing import Any

from app.storage.base import BaseVectorStore

logger = logging.getLogger(__name__)

_RRF_K = 60  # standard RRF smoothing constant


def _rrf_merge(
    dense: list[dict[str, Any]],
    sparse: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge two ranked lists with Reciprocal Rank Fusion.
    Deduplicates by chunk ID; returned list is sorted by RRF score descending.
    """
    rrf_scores: dict[str, float] = {}
    all_items: dict[str, dict[str, Any]] = {}

    for rank, item in enumerate(dense):
        id_ = item.get("id", f"dense_{rank}")
        rrf_scores[id_] = rrf_scores.get(id_, 0.0) + 1.0 / (_RRF_K + rank + 1)
        all_items[id_] = item

    for rank, item in enumerate(sparse):
        id_ = item.get("id", f"sparse_{rank}")
        rrf_scores[id_] = rrf_scores.get(id_, 0.0) + 1.0 / (_RRF_K + rank + 1)
        all_items.setdefault(id_, item)   # prefer dense item if already present

    sorted_ids = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)
    return [{**all_items[id_], "rrf_score": rrf_scores[id_]} for id_ in sorted_ids]


async def retrieve(
    query: str,
    collection: str,
    store: BaseVectorStore,
    top_n: int = 6,
    fetch_k: int = 12,
    title_filter: str = "",
) -> list[dict[str, Any]]:
    """
    Hybrid retrieval: BM25 + Chroma → RRF merge → rerank → top_n chunks.

    Args:
        top_n:        chunks returned to the LLM after reranking.
        fetch_k:      candidates fetched from each retriever before merging.
        title_filter: restrict to chunks from one paper (mirrors Chroma `where`).
    """
    from app.rag.temporary import bm25_store

    where = {"title": title_filter} if title_filter else None

    # ── 1. Run dense + sparse retrieval concurrently ──────────────────────────
    dense_task = store.query(
        collection=collection,
        query_texts=[query],
        n_results=fetch_k,
        where=where,
    )
    sparse_task = asyncio.to_thread(
        bm25_store.query, collection, query, fetch_k, title_filter
    )
    dense_results, sparse_results = await asyncio.gather(dense_task, sparse_task)

    logger.debug(
        "Hybrid retrieve: dense=%d  sparse=%d  query=%.60s",
        len(dense_results), len(sparse_results), query,
    )

    # ── 2. RRF merge ──────────────────────────────────────────────────────────
    if dense_results and sparse_results:
        candidates = _rrf_merge(dense_results, sparse_results)
    else:
        # Graceful fallback when one source is empty (e.g. BM25 file missing)
        candidates = dense_results or sparse_results

    if not candidates:
        return []

    # ── 3. Rerank the merged candidates ──────────────────────────────────────
    from app.rag.reranker.factory import get_reranker

    reranker = get_reranker()
    if reranker is not None:
        chunks = await asyncio.to_thread(reranker.rerank, query, candidates, top_n)
        logger.debug("Reranked %d → %d chunks", len(candidates), len(chunks))
    else:
        # No reranker: sort by global_chunk order for reading coherence
        candidates.sort(key=lambda c: c.get("metadata", {}).get("global_chunk", 0))
        chunks = candidates[:top_n]

    return chunks
