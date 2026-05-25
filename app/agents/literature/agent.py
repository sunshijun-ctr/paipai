import logging
import re
from typing import Any

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMMessage
from app.state.task_state import TaskState
from app.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class LiteratureAgent(BaseAgent):
    name = "literature_agent"
    description = "Searches, filters, and downloads research papers."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        input_data = agent_input.input_data
        query: str = input_data.get("query", agent_input.user_goal)
        filters: dict[str, Any] = input_data.get("filters", {})
        pre_selected: list[dict] = input_data.get("pre_selected_papers", [])
        search_only: bool = input_data.get("search_only", False)

        state.update_stage("literature_search", self.name)

        if pre_selected:
            # User specified exact papers — skip search and filter, download directly
            papers: list[dict[str, Any]] = pre_selected
            selected: list[dict[str, Any]] = pre_selected
            search_meta: dict = {}
            filter_meta: dict = {}
        else:
            max_results: int = int(filters.get("max_results", filters.get("top_k", 10)))
            sources: str = filters.get("sources", "arxiv,semantic")
            year: str = filters.get("year", filters.get("year_range", ""))
            sort_by: str = filters.get("sort_by", "relevance")
            title_search: bool = bool(filters.get("title_search", False))
            original_query = query
            query = await self._normalize_search_query(query, agent_input.user_goal)
            if query != original_query:
                logger.info("Literature search query normalized: %r -> %r", original_query, query)
            else:
                logger.info("Literature search query: %r", query)

            try:
                search_kwargs: dict[str, Any] = {
                    "query": query,
                    "max_results": max_results,
                    "sources": sources,
                    "sort_by": sort_by,
                    "title_search": title_search,
                }
                if year:
                    search_kwargs["year"] = year
                search_result = await ToolRegistry.get("search_papers").execute(**search_kwargs)
            except KeyError as exc:
                return self._error_output(agent_input, f"Tool not registered: {exc}")
            except Exception as exc:
                return self._error_output(agent_input, f"Search failed: {exc}")

            if not search_result.success:
                return self._error_output(agent_input, f"Search error: {search_result.error}")

            papers = search_result.data.get("papers", [])
            logger.info(
                "Search returned %d papers from sources %s (errors: %s)",
                len(papers),
                search_result.data.get("sources_used"),
                search_result.data.get("errors"),
            )

            if not papers:
                return AgentOutput(
                    task_id=agent_input.task_id,
                    session_id=agent_input.session_id,
                    agent_name=self.name,
                    status=AgentStatus.PARTIAL_SUCCESS,
                    result={"query": query, "papers": [], "selected_papers": []},
                    errors=["No papers found for the given query."],
                    next_suggestion="refine_query_or_change_sources",
                )

            search_meta = search_result.data

            if search_only:
                if title_search:
                    # Title search: results already sorted by title match quality, skip embedding filter
                    selected = papers[:20]
                else:
                    # Keyword search: rank by semantic similarity, show top 20
                    try:
                        filter_result = await ToolRegistry.get("filter_papers").execute(
                            papers=papers, user_goal=agent_input.user_goal, top_k=20
                        )
                        selected = filter_result.data.get("selected_papers", papers)
                        selected.sort(key=lambda p: p.get("relevance_score", 0.0), reverse=True)
                    except Exception as exc:
                        logger.warning("Search-only filter failed (%s), returning all results", exc)
                        selected = papers
                filter_meta: dict = {}
            else:
                if title_search:
                    # Title search: trust search order, take top 3 directly
                    selected = papers[:3]
                    filter_meta: dict = {}
                else:
                    # Keyword search: filter to best candidates for download
                    try:
                        filter_result = await ToolRegistry.get("filter_papers").execute(
                            papers=papers, user_goal=agent_input.user_goal, top_k=3
                        )
                        selected = filter_result.data.get("selected_papers", [])
                        filter_meta = filter_result.data
                    except KeyError as exc:
                        return self._error_output(agent_input, f"Tool not registered: {exc}")
                    except Exception as exc:
                        return self._error_output(agent_input, f"Filter failed: {exc}")

        # Search-only mode: skip download entirely
        if search_only:
            state.tool_results["literature_search"] = search_meta
            state.update_stage("search_done", self.name)
            return AgentOutput(
                task_id=agent_input.task_id,
                session_id=agent_input.session_id,
                agent_name=self.name,
                status=AgentStatus.SUCCESS,
                result={
                    "total_found": len(papers),
                    "query": query,
                    "source_counts": search_meta.get("source_counts", {}),
                    "papers": papers,
                    "selected_papers": selected,
                },
                artifacts={"downloaded_pdfs": []},
                next_suggestion="user_can_download_or_ask",
            )

        try:
            selected_normalized = [_normalize_for_download(p) for p in selected]
            download_result = await ToolRegistry.get("download_pdf").execute(papers=selected_normalized)
            downloaded: list[dict[str, Any]] = download_result.data.get("downloaded_pdfs", [])
            failed: list[dict[str, Any]] = download_result.data.get("failed", [])
        except KeyError as exc:
            return self._error_output(agent_input, f"Tool not registered: {exc}")
        except Exception as exc:
            return self._error_output(agent_input, f"Download failed: {exc}")

        state.document_list.extend(p["title"] for p in downloaded)
        if not pre_selected:
            state.tool_results["literature_search"] = search_meta
            state.tool_results["literature_filter"] = filter_meta
        state.tool_results["literature_download"] = download_result.data
        state.update_stage("literature_done", self.name)

        if downloaded:
            reply_lines = [f"已成功下载 **{len(downloaded)}** 篇论文，现在可以继续提问或下载其他文章。"]
            if failed:
                reply_lines.append("")
                reply_lines.append(f"其中有 **{len(failed)}** 篇下载失败：")
                for item in failed[:5]:
                    reply_lines.append(f"- {item.get('title', 'Untitled')}：{item.get('error', 'unknown error')}")
            reply = "\n".join(reply_lines)
            status = AgentStatus.SUCCESS if not failed else AgentStatus.PARTIAL_SUCCESS
        else:
            reply_lines = ["当前没有成功下载任何论文。你可以重试下载，或改选其他文章。"]
            if failed:
                reply_lines.append("")
                reply_lines.append(f"失败详情（共 {len(failed)} 篇）：")
                for item in failed[:5]:
                    reply_lines.append(f"- {item.get('title', 'Untitled')}：{item.get('error', 'unknown error')}")
            reply = "\n".join(reply_lines)
            status = AgentStatus.PARTIAL_SUCCESS if failed else AgentStatus.FAILED

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=status,
            result={
                "total_found": len(papers),
                "query": query,
                "source_counts": search_meta.get("source_counts", {}) if not pre_selected else {},
                "papers": papers,
                "selected_papers": selected,
                "reply": reply,
            },
            artifacts={"downloaded_pdfs": downloaded},
            next_suggestion="dispatch_to_reading_agent",
        )

    async def _normalize_search_query(self, query: str, user_goal: str) -> str:
        """Rewrite Chinese natural-language search requests into English scholarly keywords."""
        cleaned = _strip_search_noise(query)
        if not _contains_cjk(cleaned):
            return cleaned or query

        rule_based = _rule_based_search_query(cleaned)
        if rule_based:
            return rule_based

        try:
            resp = await self.llm.complete_json(
                messages=[LLMMessage(
                    role="user",
                    content=(
                        f"User request:\n{user_goal}\n\n"
                        f"Current search query:\n{query}\n\n"
                        "Return JSON only."
                    ),
                )],
                system=(
                    "You rewrite academic paper search queries for Semantic Scholar and arXiv.\n"
                    "If the user query is Chinese, translate/extract it into concise English scholarly keywords.\n"
                    "Keep domain acronyms such as HDR, 3DNR, LLM, ViT, CNN, RAG, OCR.\n"
                    "Remove words like search, papers, articles, related to, about.\n"
                    "Do not include explanations. Return only JSON: {\"search_query\": \"...\"}.\n"
                    "The search_query should be 2 to 10 English words unless an acronym is essential."
                ),
                temperature=0,
                max_tokens=80,
            )
            rewritten = str(resp.get("search_query") or "").strip()
            if rewritten and not _contains_cjk(rewritten):
                return rewritten
        except Exception as exc:
            logger.warning("Search query rewrite failed, using fallback query: %s", exc)

        return cleaned or query


def _normalize_for_download(paper: dict[str, Any]) -> dict[str, Any]:
    """Ensure downstream tools (download, reading) can find arxiv_id and pdf_url."""
    normalized = dict(paper)
    source = paper.get("source", "")
    paper_id = paper.get("paper_id", "")

    if source == "arxiv" and paper_id and "arxiv_id" not in normalized:
        normalized["arxiv_id"] = paper_id
    elif "arxiv_id" not in normalized:
        normalized["arxiv_id"] = paper_id  # best-effort for other sources

    return normalized


def _u(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _strip_search_noise(query: str) -> str:
    cleaned = (query or "").strip()
    noise = [
        _u("\\u641c\\u7d22"), _u("\\u67e5\\u627e"), _u("\\u627e"),
        _u("\\u68c0\\u7d22"), _u("\\u4e0b\\u8f7d"),
        _u("\\u5173\\u4e8e"), _u("\\u65b9\\u9762"), _u("\\u76f8\\u5173"),
        _u("\\u7684"), _u("\\u4e00\\u4e9b"), _u("\\u51e0\\u7bc7"),
        _u("\\u8bba\\u6587"), _u("\\u6587\\u732e"), _u("\\u6587\\u7ae0"),
        "paper", "papers", "article", "articles", "search", "find",
    ]
    for token in noise:
        cleaned = cleaned.replace(token, " ")
    return re.sub(r"\s+", " ", cleaned).strip(" ,，。:：;；")


def _rule_based_search_query(query: str) -> str:
    q = (query or "").lower()
    keyword_map = {
        "3dnr": "3DNR 3D noise reduction",
        "hdr": "HDR high dynamic range imaging",
        "llm": "large language models",
        "rag": "retrieval augmented generation",
        _u("\\u5927\\u6a21\\u578b"): "large language models",
        _u("\\u5927\\u8bed\\u8a00\\u6a21\\u578b"): "large language models",
        _u("\\u57fa\\u7840\\u6a21\\u578b"): "foundation models",
        _u("\\u751f\\u6210\\u5f0f\\u4eba\\u5de5\\u667a\\u80fd"): "generative artificial intelligence",
        _u("\\u591a\\u6a21\\u6001"): "multimodal learning",
        _u("\\u77e5\\u8bc6\\u56fe\\u8c31"): "knowledge graph",
        _u("\\u68c0\\u7d22\\u589e\\u5f3a\\u751f\\u6210"): "retrieval augmented generation",
        _u("\\u56fe\\u50cf\\u53bb\\u566a"): "image denoising",
        _u("\\u53bb\\u566a"): "image denoising",
        _u("\\u4e09\\u7ef4\\u53bb\\u566a"): "3D noise reduction",
        _u("\\u56fe\\u50cf\\u589e\\u5f3a"): "image enhancement",
        _u("\\u4f4e\\u5149"): "low-light image enhancement",
        _u("\\u9ad8\\u52a8\\u6001\\u8303\\u56f4"): "high dynamic range imaging",
        _u("\\u76ee\\u6807\\u68c0\\u6d4b"): "object detection",
        _u("\\u5c0f\\u76ee\\u6807"): "small object detection",
        _u("\\u8bed\\u4e49\\u5206\\u5272"): "semantic segmentation",
        _u("\\u56fe\\u50cf\\u5206\\u7c7b"): "image classification",
        _u("\\u53ef\\u53d8\\u5f62\\u6ce8\\u610f\\u529b"): "deformable attention",
        _u("\\u6ce8\\u610f\\u529b\\u673a\\u5236"): "attention mechanism",
        _u("\\u89c6\\u89c9\\u53d8\\u6362\\u5668"): "vision transformer",
        _u("\\u6269\\u6563\\u6a21\\u578b"): "diffusion models",
        _u("\\u5f3a\\u5316\\u5b66\\u4e60"): "reinforcement learning",
        _u("\\u8054\\u90a6\\u5b66\\u4e60"): "federated learning",
        _u("\\u56fe\\u795e\\u7ecf\\u7f51\\u7edc"): "graph neural networks",
        _u("\\u9065\\u611f"): "remote sensing",
        _u("\\u533b\\u5b66\\u5f71\\u50cf"): "medical imaging",
        _u("\\u8d85\\u5206\\u8fa8\\u7387"): "super resolution",
        _u("\\u5149\\u6d41"): "optical flow",
        _u("\\u4e09\\u7ef4\\u91cd\\u5efa"): "3D reconstruction",
        _u("\\u70b9\\u4e91"): "point cloud",
    }
    matches = [value for key, value in keyword_map.items() if key in q]
    if matches:
        return " ".join(dict.fromkeys(matches))

    english_tokens = [
        token
        for token in re.findall(r"(?<![A-Za-z0-9-])[A-Za-z0-9][A-Za-z0-9-]*(?![A-Za-z0-9-])", query)
        if any(ch.isalpha() for ch in token)
    ]
    if english_tokens:
        return " ".join(english_tokens[:8])
    return ""
