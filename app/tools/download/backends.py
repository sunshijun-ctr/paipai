"""
Multi-strategy PDF download backend.
Strategy order:
  1. Source-native (arxiv / biorxiv / medrxiv / iacr / semantic)
  2. Direct pdf_url from search result
  3. Unpaywall OA resolver (needs UNPAYWALL_EMAIL env var)
  4. Sci-Hub fallback (optional, default enabled)
依赖: requests, beautifulsoup4
"""
import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── arXiv ─────────────────────────────────────────────────────────────────

def _download_arxiv(paper_id: str, save_path: str) -> str:
    import requests
    os.makedirs(save_path, exist_ok=True)
    r = requests.get(f"https://arxiv.org/pdf/{paper_id}.pdf", timeout=30)
    r.raise_for_status()
    out = os.path.join(save_path, f"{paper_id.replace('/', '_')}.pdf")
    with open(out, "wb") as f:
        f.write(r.content)
    return out


# ── bioRxiv / medRxiv ─────────────────────────────────────────────────────

def _download_rxiv(doi: str, save_path: str, server: str = "biorxiv") -> str:
    import requests
    os.makedirs(save_path, exist_ok=True)
    r = requests.get(
        f"https://www.{server}.org/content/{doi}.full.pdf",
        timeout=30, allow_redirects=True,
    )
    r.raise_for_status()
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", doi)
    out = os.path.join(save_path, f"{server}_{safe}.pdf")
    with open(out, "wb") as f:
        f.write(r.content)
    return out


# ── IACR ePrint ───────────────────────────────────────────────────────────

def _download_iacr(paper_id: str, save_path: str) -> str:
    import requests
    parts = paper_id.split("/")
    year, num = (parts[0], parts[1]) if len(parts) == 2 else (paper_id[:4], paper_id[4:])
    os.makedirs(save_path, exist_ok=True)
    r = requests.get(f"https://eprint.iacr.org/{year}/{num}.pdf", timeout=30)
    r.raise_for_status()
    out = os.path.join(save_path, f"iacr_{year}_{num}.pdf")
    with open(out, "wb") as f:
        f.write(r.content)
    return out


# ── Semantic Scholar ──────────────────────────────────────────────────────

def _download_semantic(paper_id: str, save_path: str) -> str:
    import requests
    import time
    for attempt in range(3):
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}",
            params={"fields": "openAccessPdf,title"},
            timeout=30,
        )
        if r.status_code == 429:
            wait = 8 * (attempt + 1)
            logger.warning("S2 download API rate limited (429), waiting %ds", wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        break
    else:
        raise Exception(f"S2 API still rate limited after retries for paper {paper_id}")
    oa = r.json().get("openAccessPdf") or {}
    pdf_url = oa.get("url", "")
    if not pdf_url:
        raise ValueError(f"No open-access PDF for Semantic Scholar paper {paper_id}")
    os.makedirs(save_path, exist_ok=True)
    pr = requests.get(pdf_url, timeout=30)
    pr.raise_for_status()
    out = os.path.join(save_path, f"semantic_{paper_id.replace('/', '_')}.pdf")
    with open(out, "wb") as f:
        f.write(pr.content)
    return out


# ── 通用 URL 直接下载 ─────────────────────────────────────────────────────

def _download_from_url(pdf_url: str, save_path: str, filename_hint: str = "paper") -> Optional[str]:
    import requests
    if not pdf_url:
        return None
    try:
        r = requests.get(pdf_url, timeout=30, allow_redirects=True)
        if r.status_code >= 400:
            return None
        ct = r.headers.get("content-type", "").lower()
        is_pdf = "pdf" in ct or r.content[:4] == b"%PDF" or pdf_url.lower().endswith(".pdf")
        if not is_pdf:
            return None
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename_hint)[:80]
        os.makedirs(save_path, exist_ok=True)
        out = os.path.join(save_path, f"{safe}.pdf")
        with open(out, "wb") as f:
            f.write(r.content)
        return out
    except Exception as e:
        logger.warning("URL download failed %s: %s", pdf_url, e)
        return None


# ── Unpaywall ─────────────────────────────────────────────────────────────

def _resolve_unpaywall(doi: str) -> Optional[str]:
    import requests
    email = (os.environ.get("UNPAYWALL_EMAIL") or "").strip()
    if not email:
        return None
    try:
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": email},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        best = r.json().get("best_oa_location") or {}
        return best.get("url_for_pdf") or best.get("url") or None
    except Exception as e:
        logger.warning("Unpaywall error: %s", e)
        return None


# ── Sci-Hub ───────────────────────────────────────────────────────────────

def _download_scihub(identifier: str, save_path: str, base_url: str = "https://sci-hub.se") -> Optional[str]:
    import requests
    from bs4 import BeautifulSoup

    base_url = base_url.rstrip("/")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })

    def _get_pdf_url(ident: str) -> Optional[str]:
        try:
            r = session.get(f"{base_url}/{ident}", verify=False, timeout=20)
            if r.status_code != 200 or "article not found" in r.text.lower():
                return None
            soup = BeautifulSoup(r.content, "html.parser")
            for tag in soup.find_all("embed", {"type": "application/pdf"}):
                src = tag.get("src", "")
                if src:
                    return "https:" + src if src.startswith("//") else (base_url + src if src.startswith("/") else src)
            for tag in soup.find_all("iframe"):
                src = tag.get("src", "")
                if src:
                    return "https:" + src if src.startswith("//") else (base_url + src if src.startswith("/") else src)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "pdf" in href.lower() or href.endswith(".pdf"):
                    return "https:" + href if href.startswith("//") else (base_url + href if href.startswith("/") else href)
        except Exception as e:
            logger.warning("Sci-Hub parse error: %s", e)
        return None

    pdf_url = _get_pdf_url(identifier)
    if not pdf_url:
        return None
    try:
        r = session.get(pdf_url, verify=False, timeout=30)
        if r.status_code != 200:
            return None
        ct = r.headers.get("Content-Type", "").lower()
        if "application/pdf" not in ct and not r.content.startswith(b"%PDF"):
            return None
        Path(save_path).mkdir(parents=True, exist_ok=True)
        h = hashlib.md5(r.content).hexdigest()[:8]
        clean = re.sub(r"[^\w\-_.]", "_", identifier)[:60]
        out = os.path.join(save_path, f"{h}_{clean}.pdf")
        with open(out, "wb") as f:
            f.write(r.content)
        return out
    except Exception as e:
        logger.warning("Sci-Hub download error: %s", e)
        return None


# ── 原生分发 ──────────────────────────────────────────────────────────────

def _primary_download(source: str, paper_id: str, save_path: str) -> Optional[str]:
    try:
        if source == "arxiv":
            return _download_arxiv(paper_id, save_path)
        if source == "biorxiv":
            return _download_rxiv(paper_id, save_path, "biorxiv")
        if source == "medrxiv":
            return _download_rxiv(paper_id, save_path, "medrxiv")
        if source == "iacr":
            return _download_iacr(paper_id, save_path)
        if source == "semantic":
            return _download_semantic(paper_id, save_path)
    except Exception as e:
        logger.warning("Primary download failed (%s/%s): %s", source, paper_id, e)
    return None


# ── 入口 ──────────────────────────────────────────────────────────────────

async def run(inputs: dict) -> dict:
    """
    Download a single paper PDF.
    Returns {"success": bool, "local_path": str, "strategy": str, "error": str}
    """
    source: str = inputs.get("source", "").strip().lower()
    paper_id: str = inputs.get("paper_id", "").strip()
    doi: str = inputs.get("doi", "").strip()
    title: str = inputs.get("title", "").strip()
    pdf_url: str = inputs.get("pdf_url", "").strip()
    save_path: str = inputs.get("save_path", "./downloads")
    use_scihub: bool = inputs.get("use_scihub", True)
    scihub_url: str = inputs.get("scihub_base_url", "https://sci-hub.se")

    errors: list[str] = []

    # Step 1: source-native
    # Skip semantic native download if pdf_url already known — avoids a redundant S2 API call
    skip_native = source == "semantic" and bool(pdf_url)
    if source and paper_id and not skip_native:
        result = await asyncio.to_thread(_primary_download, source, paper_id, save_path)
        if result and os.path.exists(result):
            return {"success": True, "local_path": result, "strategy": f"native:{source}"}
        errors.append(f"native:{source} failed")

    # Step 2: direct pdf_url from search metadata
    if pdf_url:
        hint = re.sub(r"[^a-zA-Z0-9._-]+", "_", (paper_id or title or "paper"))[:60]
        result = await asyncio.to_thread(_download_from_url, pdf_url, save_path, hint)
        if result and os.path.exists(result):
            return {"success": True, "local_path": result, "strategy": "pdf_url"}
        errors.append("pdf_url: download failed or not a PDF")

    # Step 3: Unpaywall
    if doi:
        oa_url = await asyncio.to_thread(_resolve_unpaywall, doi)
        if oa_url:
            result = await asyncio.to_thread(_download_from_url, oa_url, save_path, f"oa_{doi}")
            if result and os.path.exists(result):
                return {"success": True, "local_path": result, "strategy": "unpaywall"}
            errors.append("unpaywall: URL resolved but download failed")
        else:
            errors.append("unpaywall: no OA URL (set UNPAYWALL_EMAIL)")
    else:
        errors.append("unpaywall: no DOI")

    # Step 4: Sci-Hub
    if use_scihub:
        identifier = doi or title or paper_id
        if identifier:
            result = await asyncio.to_thread(_download_scihub, identifier, save_path, scihub_url)
            if result and os.path.exists(result):
                return {"success": True, "local_path": result, "strategy": "scihub"}
            errors.append("scihub: failed")

    return {"success": False, "local_path": "", "strategy": "none", "error": " | ".join(errors)}
