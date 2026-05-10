"""
BM25 keyword index for hybrid retrieval.

Each Chroma collection has a paired BM25 index stored as a pickle file at:
  ./data/bm25/<collection_name>.pkl

The pickle stores a dict {doc_id → {document, metadata}}, enabling:
  - Upsert semantics (same ID → overwrite, matching Chroma's upsert)
  - BM25 index rebuilt on query from the stored corpus (fast for paper-scale)
  - title_filter to restrict search to a single paper, like Chroma's `where`
"""
import logging
import os
import pickle
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _store_path(collection: str) -> Path:
    from app.config.settings import settings
    return Path(settings.data_dir) / "bm25" / f"{collection}.pkl"


def _load(collection: str) -> dict[str, dict[str, Any]]:
    p = _store_path(collection)
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return pickle.load(f)


def _save(collection: str, docs: dict[str, dict[str, Any]]) -> None:
    p = _store_path(collection)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(docs, f)


def add(
    collection: str,
    documents: list[str],
    metadatas: list[dict[str, Any]],
    ids: list[str],
) -> None:
    """Upsert documents into the BM25 store (same ID = overwrite)."""
    docs = _load(collection)
    for id_, doc, meta in zip(ids, documents, metadatas):
        docs[id_] = {"document": doc, "metadata": meta}
    _save(collection, docs)
    logger.debug("BM25 store '%s': %d total docs after upsert", collection, len(docs))


def query(
    collection: str,
    query_text: str,
    n_results: int = 12,
    title_filter: str = "",
) -> list[dict[str, Any]]:
    """
    BM25 keyword search. Returns up to n_results items sorted by BM25 score.
    Each item: {id, document, metadata, bm25_score}
    """
    from rank_bm25 import BM25Okapi

    docs = _load(collection)
    if not docs:
        return []

    # Apply title filter (mirrors Chroma's `where={"title": ...}`)
    if title_filter:
        ids = [k for k, v in docs.items() if v["metadata"].get("title") == title_filter]
    else:
        ids = list(docs.keys())

    if not ids:
        return []

    texts = [docs[id_]["document"] for id_ in ids]
    tokenized = [t.lower().split() for t in texts]

    bm25 = BM25Okapi(tokenized)
    q_tokens = query_text.lower().split()
    scores = bm25.get_scores(q_tokens)

    # Pair (score, id) and take top-n with positive scores
    ranked = sorted(
        ((float(scores[i]), ids[i]) for i in range(len(ids)) if scores[i] > 0),
        key=lambda x: x[0],
        reverse=True,
    )[:n_results]

    return [
        {
            "id": id_,
            "document": docs[id_]["document"],
            "metadata": docs[id_]["metadata"],
            "bm25_score": score,
        }
        for score, id_ in ranked
    ]
