"""
Multi-source academic paper search backends.
Sources: arXiv, Semantic Scholar, CrossRef, PubMed.
依赖: requests, feedparser
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


# ── Paper dataclass ────────────────────────────────────────────────────────

@dataclass
class _Paper:
    paper_id: str
    title: str
    authors: List[str]
    abstract: str
    doi: str
    published_date: Optional[datetime]
    pdf_url: str
    url: str
    source: str
    categories: List[str] = field(default_factory=list)
    citations: int = 0
    venue: str = ""
    journal: str = ""

    def to_dict(self) -> Dict:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": "; ".join(self.authors) if self.authors else "",
            "abstract": self.abstract,
            "doi": self.doi,
            "published_date": self.published_date.isoformat() if self.published_date else "",
            "pdf_url": self.pdf_url,
            "url": self.url,
            "source": self.source,
            "categories": "; ".join(self.categories) if self.categories else "",
            "citations": self.citations,
            "venue": self.venue,
            "journal": self.journal,
        }


# ── 工具函数 ───────────────────────────────────────────────────────────────

def _extract_doi(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.IGNORECASE)
    return m.group(0).rstrip(".,;)") if m else ""


# ── arXiv 搜索 ─────────────────────────────────────────────────────────────

def _search_arxiv(query: str, max_results: int = 10, title_search: bool = False) -> List[_Paper]:
    import requests
    import feedparser

    session = requests.Session()
    session.headers["User-Agent"] = "paper-search-agent/1.0"

    if title_search:
        # Exact phrase match on title field
        clean = query.strip('"').strip()
        search_query = f'ti:"{clean}"'
    else:
        search_query = f"all:{query}"
    params = {
        "search_query": search_query,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }

    r = None
    for attempt in range(3):
        try:
            r = session.get("http://export.arxiv.org/api/query", params=params, timeout=30)
            if r.status_code == 200:
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep((attempt + 1) * 1.5)
        except Exception:
            time.sleep((attempt + 1) * 1.5)

    if r is None or r.status_code != 200:
        return []

    feed = feedparser.parse(r.content)
    papers = []
    for entry in feed.entries:
        try:
            authors = [a.name for a in entry.authors]
            published = datetime.strptime(entry.published, "%Y-%m-%dT%H:%M:%SZ")
            pdf_url = next((lk.href for lk in entry.links if lk.type == "application/pdf"), "")
            doi = entry.get("doi", "") or _extract_doi(entry.summary)
            papers.append(_Paper(
                paper_id=entry.id.split("/")[-1],
                title=entry.title,
                authors=authors,
                abstract=entry.summary,
                url=entry.id,
                pdf_url=pdf_url,
                published_date=published,
                source="arxiv",
                categories=[t.term for t in entry.tags],
                doi=doi,
            ))
        except Exception as e:
            logger.debug("arXiv parse error: %s", e)

    if title_search and papers:
        q = clean.lower()
        def _title_rank(p: _Paper) -> int:
            t = p.title.lower().strip()
            if t == q:
                return 0          # exact match
            if t.startswith(q) or t.endswith(q):
                return 1          # query is a prefix/suffix of title
            if q in t and len(t) - len(q) <= 15:
                return 2          # query is a short extension
            return 3              # query appears somewhere in a longer title
        papers.sort(key=_title_rank)

    return papers


# ── Semantic Scholar 搜索 ─────────────────────────────────────────────────

def _search_semantic(
    query: str,
    max_results: int = 10,
    year: Optional[str] = None,
    api_key: str = "",
    sort_by: str = "relevance",
) -> List[_Paper]:
    import requests

    api_key = api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    try:
        api_key.encode("ascii")
    except (UnicodeEncodeError, AttributeError):
        api_key = ""
    session = requests.Session()
    session.headers["User-Agent"] = random.choice([
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    ])

    params: Dict[str, Any] = {
        "query": query,
        "limit": max_results,
        "fields": "title,abstract,year,citationCount,authors,url,publicationDate,externalIds,fieldsOfStudy,openAccessPdf,venue,journal,publicationVenue",
    }
    if year:
        params["year"] = year
    # Semantic Scholar supports sort=citationCount for highly-cited results
    if sort_by == "citations":
        params["sort"] = "citationCount"
    elif sort_by == "date":
        params["sort"] = "publicationDate"

    headers = {"x-api-key": api_key} if api_key else {}

    for attempt in range(3):
        try:
            r = session.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params=params, headers=headers, timeout=30,
            )
            if r.status_code == 429:
                wait = 2 if api_key else 8   # with key: brief wait; without: longer
                logger.warning("S2 rate limited (429), waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            if r.status_code == 403 and api_key:
                headers = {}
                continue
            r.raise_for_status()
            break
        except Exception as e:
            if attempt == 2:
                logger.warning("Semantic Scholar error: %s", e)
                return []
            time.sleep(3)
    else:
        return []

    papers = []
    for item in r.json().get("data", [])[:max_results]:
        try:
            authors = [a["name"] for a in item.get("authors", [])]
            pub_date = None
            if item.get("publicationDate"):
                try:
                    pub_date = datetime.strptime(item["publicationDate"], "%Y-%m-%d")
                except Exception:
                    pass

            oa = item.get("openAccessPdf") or {}
            pdf_url = oa.get("url", "")

            ext = item.get("externalIds") or {}
            doi = ext.get("DOI", "") or _extract_doi(item.get("abstract", ""))
            journal_info = item.get("journal") or {}
            publication_venue = item.get("publicationVenue") or {}
            venue = (
                item.get("venue")
                or publication_venue.get("name")
                or journal_info.get("name")
                or ""
            )

            papers.append(_Paper(
                paper_id=item["paperId"],
                title=item.get("title", ""),
                authors=authors,
                abstract=item.get("abstract", ""),
                url=item.get("url", ""),
                pdf_url=pdf_url,
                published_date=pub_date,
                source="semantic",
                categories=item.get("fieldsOfStudy") or [],
                doi=doi,
                citations=item.get("citationCount", 0),
                venue=venue,
                journal=journal_info.get("name", "") if isinstance(journal_info, dict) else "",
            ))
        except Exception as e:
            logger.debug("Semantic parse error: %s", e)
    return papers


def _enrich_arxiv_citations(papers: List[Dict], api_key: str = "") -> List[Dict]:
    """Look up citation counts for arXiv papers via Semantic Scholar batch API."""
    import requests

    arxiv_papers = [p for p in papers if p.get("source") == "arxiv" and p.get("paper_id")]
    if not arxiv_papers:
        return papers

    ids = [f"arXiv:{p['paper_id']}" for p in arxiv_papers]
    headers = {"x-api-key": api_key} if api_key else {}

    try:
        r = requests.post(
            "https://api.semanticscholar.org/graph/v1/paper/batch",
            params={"fields": "citationCount,externalIds"},
            json={"ids": ids},
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug("S2 batch enrichment failed: %s", e)
        return papers

    # Build arxiv_id → citation count map
    cite_map: Dict[str, int] = {}
    for item in data:
        if not item:
            continue
        ext = item.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv", "")
        if arxiv_id:
            cite_map[arxiv_id] = item.get("citationCount", 0)

    # Patch citation counts back into the paper dicts
    for p in papers:
        if p.get("source") == "arxiv":
            pid = p.get("paper_id", "")
            if pid in cite_map:
                p["citations"] = cite_map[pid]

    return papers


# ── CrossRef 搜索 ─────────────────────────────────────────────────────────

def _search_crossref(query: str, max_results: int = 10) -> List[_Paper]:
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "paper-search-agent/1.0 (mailto:agent@example.org)"

    params = {
        "query": query,
        "rows": min(max_results, 100),
        "sort": "relevance",
        "order": "desc",
        "mailto": "agent@example.org",
    }

    try:
        r = session.get("https://api.crossref.org/works", params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2)
            r = session.get("https://api.crossref.org/works", params=params, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logger.warning("CrossRef error: %s", e)
        return []

    papers = []
    for item in r.json().get("message", {}).get("items", []):
        try:
            doi = item.get("DOI", "")
            titles = item.get("title", [])
            title = titles[0] if titles else ""

            authors = []
            for a in item.get("author", []):
                given, family = a.get("given", ""), a.get("family", "")
                if given and family:
                    authors.append(f"{given} {family}")
                elif family:
                    authors.append(family)

            pub_date = None
            for df in ("published", "issued", "created"):
                dp = item.get(df, {}).get("date-parts", [[]])
                if dp and dp[0]:
                    parts = dp[0]
                    try:
                        pub_date = datetime(
                            parts[0] if len(parts) > 0 else 1970,
                            parts[1] if len(parts) > 1 else 1,
                            parts[2] if len(parts) > 2 else 1,
                        )
                        break
                    except Exception:
                        pass

            pdf_url = ""
            for link in item.get("link", []):
                if "pdf" in link.get("content-type", "").lower():
                    pdf_url = link.get("URL", "")
                    break

            papers.append(_Paper(
                paper_id=doi,
                title=title,
                authors=authors,
                abstract=item.get("abstract", ""),
                doi=doi,
                published_date=pub_date or datetime(1970, 1, 1),
                pdf_url=pdf_url,
                url=item.get("URL", f"https://doi.org/{doi}" if doi else ""),
                source="crossref",
                categories=[item.get("type", "")],
                citations=item.get("is-referenced-by-count", 0),
                venue=(item.get("container-title") or [""])[0] if item.get("container-title") else "",
                journal=(item.get("container-title") or [""])[0] if item.get("container-title") else "",
            ))
        except Exception as e:
            logger.debug("CrossRef parse error: %s", e)
    return papers


# ── PubMed 搜索 ───────────────────────────────────────────────────────────

def _search_pubmed(query: str, max_results: int = 10) -> List[_Paper]:
    import requests

    SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    try:
        r = requests.get(SEARCH_URL, params={
            "db": "pubmed", "term": query,
            "retmax": max_results, "retmode": "xml",
        }, timeout=30)
        ids = [el.text for el in ET.fromstring(r.content).findall(".//Id") if el.text]
        if not ids:
            return []

        r2 = requests.get(FETCH_URL, params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "xml",
        }, timeout=30)
        root = ET.fromstring(r2.content)
    except Exception as e:
        logger.warning("PubMed error: %s", e)
        return []

    papers = []
    for article in root.findall(".//PubmedArticle"):
        try:
            pmid = (article.find(".//PMID").text or "").strip()
            title_el = article.find(".//ArticleTitle")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            if not title:
                continue

            authors = []
            for a in article.findall(".//Author"):
                ln = getattr(a.find("LastName"), "text", "") or ""
                init = getattr(a.find("Initials"), "text", "") or ""
                if ln:
                    authors.append(f"{ln} {init}".strip())

            abstract = " ".join(
                "".join(ab.itertext()).strip()
                for ab in article.findall(".//AbstractText")
                if "".join(ab.itertext()).strip()
            )

            year_el = article.find(".//PubDate/Year")
            pub_date = None
            if year_el is not None and year_el.text:
                try:
                    pub_date = datetime(int(year_el.text), 1, 1)
                except Exception:
                    pass

            doi_el = article.find('.//ELocationID[@EIdType="doi"]')
            doi = doi_el.text if doi_el is not None else _extract_doi(abstract)
            journal_el = article.find(".//Journal/Title")
            journal = "".join(journal_el.itertext()).strip() if journal_el is not None else ""

            papers.append(_Paper(
                paper_id=pmid,
                title=title,
                authors=authors,
                abstract=abstract,
                doi=doi,
                published_date=pub_date,
                pdf_url="",
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                source="pubmed",
                venue=journal,
                journal=journal,
            ))
        except Exception as e:
            logger.debug("PubMed parse error: %s", e)
    return papers


# ── 去重 ──────────────────────────────────────────────────────────────────

def _dedupe(papers: List[Dict]) -> List[Dict]:
    seen: set = set()
    out: List[Dict] = []
    for p in papers:
        doi = (p.get("doi") or "").strip().lower()
        key = f"doi:{doi}" if doi else f"title:{(p.get('title') or '').strip().lower()}"
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ── 入口 ──────────────────────────────────────────────────────────────────

_SEARCHERS = {
    "arxiv":    lambda q, n, **kw: _search_arxiv(q, n, title_search=kw.get("title_search", False)),
    "semantic": lambda q, n, **kw: _search_semantic(q, n, year=kw.get("year"), api_key=kw.get("api_key", ""), sort_by=kw.get("sort_by", "relevance")),
    "crossref": lambda q, n, **_: _search_crossref(q, n),
    "pubmed":   lambda q, n, **_: _search_pubmed(q, n),
}

AVAILABLE_SOURCES = list(_SEARCHERS.keys())


async def run(inputs: dict) -> str:
    """执行搜索，返回 JSON 字符串。"""
    query: str = inputs["query"]
    sources_str: str = inputs.get("sources", "arxiv,semantic")
    max_results: int = int(inputs.get("max_results", 10))
    year: Optional[str] = inputs.get("year") or None
    api_key: str = inputs.get("semantic_scholar_api_key", "")
    sort_by: str = inputs.get("sort_by", "relevance")
    title_search: bool = bool(inputs.get("title_search", False))

    if sources_str.strip().lower() == "all":
        sources = AVAILABLE_SOURCES
    else:
        sources = [s.strip().lower() for s in sources_str.split(",") if s.strip().lower() in _SEARCHERS]

    if not sources:
        return json.dumps({"error": f"No valid sources. Available: {', '.join(AVAILABLE_SOURCES)}"})

    tasks = {
        src: asyncio.to_thread(_SEARCHERS[src], query, max_results, year=year, api_key=api_key, sort_by=sort_by, title_search=title_search)
        for src in sources
    }
    outputs = await asyncio.gather(*tasks.values(), return_exceptions=True)

    all_papers: List[Dict] = []
    errors: Dict[str, str] = {}
    source_counts: Dict[str, int] = {}

    for src, result in zip(tasks.keys(), outputs):
        if isinstance(result, Exception):
            errors[src] = str(result)
            source_counts[src] = 0
        else:
            dicts = [p.to_dict() for p in result]
            source_counts[src] = len(dicts)
            all_papers.extend(dicts)

    # Auto-fallback: if semantic was the only source and returned nothing, try arXiv
    if not all_papers and "semantic" in sources and "arxiv" not in sources:
        logger.info("Semantic Scholar returned 0 results, falling back to arXiv...")
        try:
            fallback = await asyncio.to_thread(_search_arxiv, query, max_results, title_search)
            fallback_dicts = [p.to_dict() for p in fallback]
            if fallback_dicts:
                all_papers.extend(fallback_dicts)
                source_counts["arxiv"] = len(fallback_dicts)
                sources = list(sources) + ["arxiv"]
                logger.info("arXiv fallback found %d papers", len(fallback_dicts))
        except Exception as e:
            errors["arxiv_fallback"] = str(e)

    deduped = _dedupe(all_papers)

    # Enrich arXiv results with citation counts from S2, then sort
    if sort_by == "citations":
        deduped = await asyncio.to_thread(_enrich_arxiv_citations, deduped, api_key)
        deduped.sort(key=lambda p: p.get("citations", 0), reverse=True)
    elif sort_by == "date":
        deduped.sort(key=lambda p: p.get("published_date") or "", reverse=True)

    return json.dumps(
        {
            "query": query,
            "total": len(deduped),
            "sources_used": sources,
            "source_counts": source_counts,
            "sort_by": sort_by,
            "errors": errors,
            "papers": deduped,
        },
        ensure_ascii=False,
        indent=2,
    )
