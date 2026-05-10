import json
import logging
import re

from app.agents.base.agent import BaseAgent
from app.config.settings import settings
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMMessage
from app.state.task_state import TaskState
from app.tools.web.common import (
    extract_urls,
    fetch_url_metadata,
    gather_limited,
    scrape_url,
    should_bypass_web_cache,
    web_cache_key,
)
from app.tools.web.search_tool import search_web_with_fallback

logger = logging.getLogger(__name__)

_MATH_RULES = """\
When your answer includes mathematical formulas, follow these rules:
- Inline formulas: use $...$
- Block / display formulas: use $$...$$
- Never output bare LaTeX without delimiters.
- Never place formulas inside code blocks unless the user asks to see raw source.
- Write subscripts and superscripts completely, e.g. \\mathbb{E}_{x,c} not \\mathbb{E}{x,c}.\
"""

_WEB_READING_SYSTEM = f"""
你是一位网页搜索与阅读助手。你会先基于用户问题召回网页，再基于抓取到的网页正文综合回答。

通用原则：
- 不要只罗列链接；必须阅读抓取到的网页正文并给出综合回答。
- 只使用提供的网页内容作为依据；网页没有提供的信息要明确说明“当前网页内容无法支持该结论”。
- 多个来源之间有差异时，指出差异并说明来自哪个来源。
- 如果内容涉及近期信息，优先说明网页中能看到的时间、版本或发布信息。
- 开源代码/项目类问题要优先提炼项目用途、核心能力、技术栈、活跃度线索、适合场景和链接。
- 保留关键链接、术语、模型名称、代码/API 名称和指标。
- 优先中文回答；用户用英文提问时用英文。

引用方式：
- 需要引用依据时，用“来源 1/来源 2”这样的来源标注，不要编造网页中不存在的出处。
- 末尾可给一个“可继续查看的链接”短列表，但主体必须是阅读后的总结。

{_MATH_RULES}
"""


class WebAgent(BaseAgent):
    name = "web_agent"
    description = "Searches the web, scrapes selected pages, and answers from web page content."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        question = str(agent_input.input_data.get("question") or agent_input.user_goal or "").strip()
        urls = [str(u).strip() for u in agent_input.input_data.get("urls", []) if str(u).strip()]
        search_results: list[dict] = []

        if not question:
            return self._error_output(agent_input, "缺少网页搜索/阅读问题。")

        if not urls:
            urls = extract_urls(question)

        if not urls:
            try:
                state.update_stage("web_search", self.name)
                max_results = max(settings.web_read_max_urls * 2, 10)
                search_query = _build_web_search_query(question)
                search_results = await search_web_with_fallback(search_query, question, max_results)
                urls = [item.get("url", "") for item in search_results if item.get("url")]
            except Exception as exc:
                logger.exception("Web search failed: %s", exc)
                return self._error_output(agent_input, f"网页搜索失败：{exc}")

        if not urls:
            return self._error_output(agent_input, "没有找到可读取的网页 URL。")

        state.update_stage("web_fetch", self.name)
        meta_results = await gather_limited([fetch_url_metadata(url, question) for url in urls], limit=5)
        metas = [m for m in meta_results if not isinstance(m, Exception)]
        metas.sort(key=lambda item: (item.get("recommended", False), item.get("score", 0)), reverse=True)
        targets = [m for m in metas if m.get("accessible") and m.get("content_type") != "pdf"]
        if not targets:
            targets = [{"url": url, "title": url, "description": "", "content_type": "article", "recommended": True} for url in urls]
        targets = targets[: max(1, settings.web_read_max_urls)]

        state.update_stage("web_reading", self.name)
        bypass_cache = should_bypass_web_cache(question)
        scraped = await gather_limited(
            [_scrape_web_page_cached(item["url"], bypass_cache) for item in targets],
            limit=settings.web_read_max_urls,
        )
        pages = [p for p in scraped if not isinstance(p, Exception) and p.get("text")]
        if not pages:
            return self._error_output(agent_input, "网页正文抓取失败，可能是页面不可访问或反爬限制。")

        context_parts, selected_images = _merge_web_pages(pages, question, settings.web_read_max_chars)
        context = "\n\n".join(context_parts)
        image_note = _format_image_note(selected_images)
        if image_note:
            context = f"{context}\n\n{image_note}"

        try:
            resp = await self.llm.complete(
                messages=[LLMMessage(
                    role="user",
                    content=f"Web pages:\n{context}\n\nQuestion: {question}",
                )],
                system=_WEB_READING_SYSTEM,
            )
            answer = resp.content
        except Exception as exc:
            logger.exception("LLM call failed in WebAgent: %s", exc)
            answer = "[网页抓取成功但 LLM 调用失败，请检查 LLM 配置]"

        state.update_stage("web_done", self.name)
        metadata = {
            "retriever": "web_agent",
            "url_count": len(pages),
            "urls": [p.get("url", "") for p in pages],
            "search_results": search_results,
            "images": selected_images,
            "cache_bypassed": bypass_cache,
        }
        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result={
                "web_notes": [{
                    "title": " | ".join(p.get("title", p.get("url", "")) for p in pages),
                    "question": question,
                    "answer": answer,
                    "contexts": context_parts,
                    "metadata": metadata,
                }],
                "question": question,
                "answer": answer,
                "contexts": context_parts,
                "metadata": metadata,
            },
            artifacts={"source": "web"},
            next_suggestion="continue_web_reading_or_summarize",
        )


async def _scrape_web_page_cached(url: str, bypass_cache: bool) -> dict:
    cache_key = web_cache_key(url)
    if not bypass_cache:
        try:
            from app.storage.factory import get_kv_store
            cached = await get_kv_store().get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    data = await scrape_url(url, extract_images=True, extract_code=True)
    try:
        from app.storage.factory import get_kv_store
        await get_kv_store().set(cache_key, json.dumps(data, ensure_ascii=False), ttl=24 * 3600)
    except Exception:
        pass
    return data


def _build_web_search_query(question: str) -> str:
    q = (question or "").strip()
    lower = q.lower()
    code_markers = [
        "开源代码", "开源项目", "源代码", "源码", "代码仓库", "仓库",
        "github", "gitlab", "repo", "repository", "codebase",
        "open source", "open-source",
    ]
    if any(marker in lower for marker in code_markers):
        if "github" not in lower:
            return f"{q} GitHub repository open source code"
        return f"{q} repository open source code"
    return q


def _merge_web_pages(pages: list[dict], question: str, total_budget: int) -> tuple[list[str], list[dict]]:
    per_page_budget = max(4000, total_budget // max(len(pages), 1))
    parts: list[str] = []
    images: list[dict] = []
    for idx, page in enumerate(pages, start=1):
        text = _select_relevant_web_text(page.get("text", ""), question, per_page_budget)
        code_note = _format_code_blocks(page.get("code_blocks", []), question)
        page_images = [
            {**img, "source_url": page.get("url", ""), "source_index": idx}
            for img in page.get("images", [])
            if img.get("worth_reading")
        ]
        images.extend(page_images)
        source = (
            f"## 来源 {idx}: {page.get('title') or page.get('url')}\n"
            f"URL: {page.get('url')}\n\n"
            f"{text}"
        )
        if code_note:
            source = f"{source}\n\n{code_note}"
        parts.append(source[:per_page_budget + 3000])

    images.sort(key=lambda item: item.get("score", 0), reverse=True)
    return parts, images[:3]


def _select_relevant_web_text(text: str, question: str, budget: int) -> str:
    text = (text or "").strip()
    if len(text) <= budget:
        return text
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    q_terms = _q_tokens(question)
    scored: list[tuple[int, int, str]] = []
    for idx, para in enumerate(paragraphs):
        lower = para.lower()
        score = sum(1 for term in q_terms if term.lower() in lower)
        if idx < 4:
            score += 2
        scored.append((score, -idx, para))
    selected = sorted(scored, reverse=True)
    out: list[tuple[int, str]] = []
    used = 0
    for _, neg_idx, para in selected:
        if used >= budget:
            break
        room = budget - used
        clipped = para[:room]
        out.append((-neg_idx, clipped))
        used += len(clipped) + 2
    out.sort(key=lambda item: item[0])
    return "\n\n".join(para for _, para in out)


def _format_code_blocks(blocks: list[dict], question: str) -> str:
    if not blocks:
        return ""
    q_terms = _q_tokens(question)
    ranked = []
    for block in blocks:
        content = block.get("content", "")
        score = sum(1 for term in q_terms if term.lower() in content.lower())
        ranked.append((score, block))
    ranked.sort(key=lambda item: item[0], reverse=True)
    chunks: list[str] = []
    for _, block in ranked[:3]:
        content = block.get("content", "")
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[:200]
        lang = block.get("language", "")
        chunks.append(f"```{lang}\n" + "\n".join(lines) + "\n```")
    return "### 相关代码块\n\n" + "\n\n".join(chunks)


def _format_image_note(images: list[dict]) -> str:
    if not images:
        return ""
    lines = ["## 候选内容图片（仅含网页可见的 alt/caption 信息）"]
    for idx, img in enumerate(images, start=1):
        desc = img.get("caption") or img.get("alt") or "无说明"
        lines.append(f"- 图片 {idx}（来源 {img.get('source_index')}）：{desc} URL: {img.get('url')}")
    return "\n".join(lines)


def _q_tokens(text: str) -> set[str]:
    english = {w for w in re.findall(r"[a-zA-Z]{3,}", text.lower())}
    chinese_spans = re.findall(r"[一-鿿]+", text)
    bigrams: set[str] = set()
    for span in chinese_spans:
        if len(span) == 1:
            bigrams.add(span)
        else:
            bigrams.update(span[i:i + 2] for i in range(len(span) - 1))
    return english | bigrams
