import asyncio
import os
from collections import OrderedDict
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query


router = APIRouter(prefix="/api/citation", tags=["citation"])

SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
GRAPH_FIELDS = "paperId,title,year,citationCount,authors,url"
PDF_FIELDS = "paperId,title,openAccessPdf,externalIds,url"
REFERENCE_FIELDS = (
    "citedPaper.paperId,citedPaper.title,citedPaper.year,"
    "citedPaper.citationCount,citedPaper.authors,citedPaper.url"
)
CITATION_FIELDS = (
    "citingPaper.paperId,citingPaper.title,citingPaper.year,"
    "citingPaper.citationCount,citingPaper.authors,citingPaper.url"
)

_CACHE: OrderedDict[str, Any] = OrderedDict()
_CACHE_MAX_ITEMS = 128


def _headers() -> dict[str, str]:
    headers = {"User-Agent": "ResearchAgent/1.0"}
    api_key = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError:
        api_key = ""
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _cache_get(key: str) -> Any | None:
    if key not in _CACHE:
        return None
    value = _CACHE.pop(key)
    _CACHE[key] = value
    return value


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = value
    while len(_CACHE) > _CACHE_MAX_ITEMS:
        _CACHE.popitem(last=False)


async def _s2_get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict:
    try:
        resp = await client.get(
            f"{SEMANTIC_SCHOLAR_API}{path}",
            params=params,
            headers=_headers(),
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Semantic Scholar request timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Semantic Scholar network error: {exc}") from exc
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Semantic Scholar API rate limited")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="paper not found")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Semantic Scholar request failed")
    return resp.json()


def _paper_node(paper: dict, *, is_root: bool = False) -> dict | None:
    paper_id = paper.get("paperId")
    if not paper_id:
        return None
    authors = paper.get("authors") or []
    return {
        "id": paper_id,
        "title": paper.get("title") or "Unknown",
        "year": paper.get("year"),
        "citationCount": paper.get("citationCount") or 0,
        "authors": [a.get("name", "") for a in authors[:3] if a.get("name")],
        "url": paper.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}",
        "isRoot": is_root,
    }


async def _load_neighbors(
    client: httpx.AsyncClient,
    paper_id: str,
    *,
    limit: int,
) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []

    refs_task = _s2_get(
        client,
        f"/paper/{paper_id}/references",
        {"fields": REFERENCE_FIELDS, "limit": limit},
    )
    cites_task = _s2_get(
        client,
        f"/paper/{paper_id}/citations",
        {"fields": CITATION_FIELDS, "limit": limit},
    )
    results = await asyncio.gather(refs_task, cites_task, return_exceptions=True)
    references = (results[0].get("data") or []) if isinstance(results[0], dict) else []
    citations = (results[1].get("data") or []) if isinstance(results[1], dict) else []

    for ref in references:
        node = _paper_node(ref.get("citedPaper") or {})
        if not node:
            continue
        nodes.append(node)
        edges.append({"source": paper_id, "target": node["id"], "type": "references"})

    for cite in citations:
        node = _paper_node(cite.get("citingPaper") or {})
        if not node:
            continue
        nodes.append(node)
        edges.append({"source": node["id"], "target": paper_id, "type": "citations"})

    return nodes, edges


@router.get("/search")
async def search_paper_id(title: str = Query(..., min_length=1)):
    cache_key = f"search:{title.strip().lower()}"
    if cached := _cache_get(cache_key):
        return cached

    async with httpx.AsyncClient(timeout=20) as client:
        data = await _s2_get(
            client,
            "/paper/search",
            {"query": title, "limit": 1, "fields": GRAPH_FIELDS},
        )

    papers = data.get("data") or []
    if not papers:
        raise HTTPException(status_code=404, detail="paper not found")
    result = papers[0]
    _cache_set(cache_key, result)
    return result


@router.get("/pdf/{paper_id}")
async def get_citation_pdf(paper_id: str):
    cache_key = f"pdf:{paper_id}"
    if cached := _cache_get(cache_key):
        return cached

    async with httpx.AsyncClient(timeout=20) as client:
        data = await _s2_get(client, f"/paper/{paper_id}", {"fields": PDF_FIELDS})

    title = data.get("title") or "paper"
    open_access = data.get("openAccessPdf") or {}
    pdf_url = open_access.get("url") or ""
    source = "openAccessPdf" if pdf_url else ""

    external_ids = data.get("externalIds") or {}
    arxiv_id = external_ids.get("ArXiv") or external_ids.get("ARXIV")
    if not pdf_url and arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        source = "arxiv"

    if not pdf_url:
        raise HTTPException(status_code=404, detail="no open-access PDF found for this paper")

    result = {
        "paperId": data.get("paperId") or paper_id,
        "title": title,
        "url": pdf_url,
        "source": source,
    }
    _cache_set(cache_key, result)
    return result


@router.get("/graph/{paper_id}")
async def get_citation_graph(
    paper_id: str,
    depth: int = Query(1, ge=1, le=2),
    limit: int = Query(20, ge=1, le=50),
):
    cache_key = f"graph:{paper_id}:{depth}:{limit}"
    if cached := _cache_get(cache_key):
        return cached

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        root = await _s2_get(client, f"/paper/{paper_id}", {"fields": GRAPH_FIELDS})
        root_node = _paper_node(root, is_root=True)
        if not root_node:
            raise HTTPException(status_code=404, detail="paper not found")
        root_id = root_node["id"]
        nodes[root_id] = root_node

        first_nodes, first_edges = await _load_neighbors(client, root_id, limit=limit)
        for node in first_nodes:
            nodes[node["id"]] = node
        edges.extend(first_edges)

        if depth > 1:
            frontier = [node["id"] for node in first_nodes[: min(len(first_nodes), 8)]]
            nested_limit = max(3, min(limit // 4, 8))
            nested = await asyncio.gather(
                *(_load_neighbors(client, pid, limit=nested_limit) for pid in frontier),
                return_exceptions=True,
            )
            for item in nested:
                if not isinstance(item, tuple):
                    continue
                nested_nodes, nested_edges = item
                for node in nested_nodes:
                    nodes.setdefault(node["id"], node)
                edges.extend(nested_edges)

    result = {"nodes": list(nodes.values()), "edges": edges, "root": root_id}
    _cache_set(cache_key, result)
    return result
