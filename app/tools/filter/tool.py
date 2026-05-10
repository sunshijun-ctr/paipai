import asyncio
import logging
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class FilterTool(BaseTool):
    name = "filter_papers"
    description = "Filter and rank papers by semantic relevance to the user's query."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "papers": {"type": "array", "description": "List of paper dicts"},
                "user_goal": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["papers", "user_goal"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        papers: list[dict] = kwargs.get("papers", [])
        user_goal: str = kwargs.get("user_goal", "")
        top_k: int = int(kwargs.get("top_k", 5))

        if not papers:
            return ToolResult(success=True, data={"selected_papers": [], "total_input": 0, "total_selected": 0})

        if len(papers) <= top_k:
            return ToolResult(
                success=True,
                data={"selected_papers": papers, "total_input": len(papers), "total_selected": len(papers)},
            )

        try:
            ranked = await asyncio.to_thread(_rank_papers, papers, user_goal, top_k)
            method = ranked[0].get("_filter_method", "unknown") if ranked else "unknown"
            logger.info("FilterTool: ranked %d → %d papers via %s", len(papers), len(ranked), method)
            for p in ranked:
                p.pop("_filter_method", None)
        except Exception as exc:
            logger.warning("FilterTool ranking failed (%s), falling back to top-k", exc)
            ranked = papers[:top_k]

        return ToolResult(
            success=True,
            data={"selected_papers": ranked, "total_input": len(papers), "total_selected": len(ranked)},
        )


# ── Ranking logic (runs in a thread) ──────────────────────────────────────────

def _rank_papers(papers: list[dict], query: str, top_k: int) -> list[dict]:
    q = query.strip().lower()

    # Exact title matches always go first, guaranteed regardless of embedding score
    exact = [p for p in papers if p.get("title", "").strip().lower() == q]
    rest = [p for p in papers if p.get("title", "").strip().lower() != q]

    remaining_k = max(0, top_k - len(exact))
    if remaining_k == 0 or not rest:
        return (exact + rest)[:top_k]

    try:
        ranked_rest = _rank_with_embeddings(rest, query, remaining_k)
    except Exception as exc:
        logger.warning("Embedding filter failed (%s), using BM25 fallback", exc)
        ranked_rest = _rank_with_bm25(rest, query, remaining_k)

    return exact + ranked_rest


def _paper_text(paper: dict) -> str:
    title = paper.get("title", "")
    abstract = paper.get("abstract", "") or ""
    return f"{title} {abstract[:600]}".strip()


def _rank_with_embeddings(papers: list[dict], query: str, top_k: int) -> list[dict]:
    import numpy as np

    embed_fn = _get_embed_fn()
    texts = [_paper_text(p) for p in papers]
    all_texts = texts + [query]

    embeddings = embed_fn(all_texts)
    emb = np.array(embeddings, dtype=np.float32)

    paper_embs = emb[:-1]
    query_emb = emb[-1]

    # Cosine similarity
    paper_norms = np.linalg.norm(paper_embs, axis=1, keepdims=True)
    query_norm_val = np.linalg.norm(query_emb)
    paper_embs_normed = paper_embs / np.maximum(paper_norms, 1e-9)
    query_emb_normed = query_emb / max(query_norm_val, 1e-9)
    scores = paper_embs_normed @ query_emb_normed

    top_indices = scores.argsort()[::-1][:top_k]

    result = []
    for i in top_indices:
        paper = dict(papers[i])
        paper["relevance_score"] = float(scores[i])
        paper["selection_reason"] = f"Semantic similarity: {scores[i]:.3f}"
        paper["_filter_method"] = "embedding"
        result.append(paper)
    return result


def _rank_with_bm25(papers: list[dict], query: str, top_k: int) -> list[dict]:
    from rank_bm25 import BM25Okapi

    corpus = [_paper_text(p).lower().split() for p in papers]
    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query.lower().split())

    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    result = []
    for i in top_indices:
        paper = dict(papers[i])
        paper["relevance_score"] = float(scores[i])
        paper["selection_reason"] = f"BM25 keyword score: {scores[i]:.3f}"
        paper["_filter_method"] = "bm25"
        result.append(paper)
    return result


# ── Embedding function singleton ───────────────────────────────────────────────

_embed_fn = None

def _get_embed_fn():
    global _embed_fn
    if _embed_fn is not None:
        return _embed_fn

    # Try chromadb's bundled ONNX MiniLM-L6-v2 (no extra install needed)
    try:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        _embed_fn = DefaultEmbeddingFunction()
        logger.info("FilterTool: using Chroma DefaultEmbeddingFunction (ONNX MiniLM-L6-v2)")
        return _embed_fn
    except Exception:
        pass

    # Try the explicit ONNX class (chromadb >= 0.5 alternate import path)
    try:
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        _embed_fn = ONNXMiniLM_L6_V2()
        logger.info("FilterTool: using ONNXMiniLM_L6_V2")
        return _embed_fn
    except Exception:
        pass

    raise RuntimeError("No embedding function available — will use BM25 fallback")
