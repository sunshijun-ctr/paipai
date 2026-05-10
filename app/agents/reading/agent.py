import hashlib
import json
import logging
import os
import re

from app.agents.base.agent import BaseAgent
from app.config.settings import settings
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMMessage
from app.state.task_state import TaskState
from app.tools.web.common import extract_urls, fetch_url_metadata, gather_limited, scrape_url, should_bypass_web_cache, web_cache_key
from app.tools.web.search_tool import search_web_with_fallback

logger = logging.getLogger(__name__)

# ── ReadingAgent ───────────────────────────────────────────────────────────
# 职责：从文档中生成回答（两种模式）
# 模式1: direct_pdf
#   - 直接读取本地 PDF 文件，使用 _READING_SYSTEM 提示词（针对完整原文）
# 模式2: library_qa (via RetrievalAgent)
#   - 从 RetrievalAgent 获取已检索的chunks，使用 _LIBRARY_SYSTEM 提示词（针对片段）
# ───────────────────────────────────────────────────────────────────────────
_QA_TTL = 1800       # cache LLM answers for 30 minutes
_PDF_TTL = 24 * 3600


def _pdf_cache_key(local_path: str) -> str:
    mtime = int(os.path.getmtime(local_path))
    tag = hashlib.md5(local_path.encode()).hexdigest()[:12]
    return f"ra:pdf:{tag}:{mtime}"


def _qa_cache_key(local_path: str, question: str) -> str:
    mtime = int(os.path.getmtime(local_path))
    path_tag = hashlib.md5(local_path.encode()).hexdigest()[:12]
    q_tag = hashlib.md5(question.encode()).hexdigest()[:12]
    return f"ra:qa:{path_tag}:{mtime}:{q_tag}"

# Max characters to send to the LLM in one call (~50k ≈ 12k tokens, safe for most models)
_MAX_CHARS = 50_000

_MATH_RULES = """\
When your answer includes mathematical formulas, follow these rules:
- Inline formulas: use $...$
- Block / display formulas: use $$...$$
- Never output bare LaTeX without delimiters.
- Never place formulas inside code blocks unless the user asks to see raw source.
- Write subscripts and superscripts completely, e.g. \\mathbb{E}_{x,c} not \\mathbb{E}{x,c}.\
"""

_READING_SYSTEM = f"""
你是一位科研论文阅读助手。你会基于提供的论文原文回答用户的具体问题。

通用原则：
- 直接回答用户问的问题，不要套固定论文总览模板。
- 只使用提供的论文内容作为依据；论文未提供的信息要明确说明“论文未提供该信息”。
- 可以解释、重组、归纳论文内容，但不要引入论文之外的具体事实、数据或实验结果。
- 根据问题类型自然组织答案，标题和段落数量由问题决定。
- 不要为了格式完整而回答用户没问的背景、方法、实验或局限。
- 如果用户只问一个概念、一个公式、一个实验结果或一个局部细节，就只围绕该问题回答。
- 只有当用户明确要求“总结全文/介绍这篇论文/系统讲解”时，才可以使用结构化分段。

风格要求：
- 优先中文回答；用户用英文提问时用英文。
- 保留关键术语、模型名称、公式符号和实验指标。
- 回答要清楚、克制、可验证，不要机械复述原文。

{_MATH_RULES}
"""

_LIBRARY_SYSTEM = f"""
你是一位科研论文阅读助手。你会基于检索到的论文片段回答用户的具体问题。

通用原则：
- 直接回答用户问的问题，不要套固定论文总览模板。
- 只使用提供的片段作为依据；片段不足时明确说明“当前片段无法支持该结论”。
- 可以解释、重组、归纳片段内容，但不要引入片段之外的具体事实、数据或实验结果。
- 根据问题类型自然组织答案，标题和段落数量由问题决定。
- 不要为了格式完整而回答用户没问的背景、方法、实验或局限。
- 如果用户只问一个概念、一个公式、一个实验结果或一个局部细节，就只围绕该问题回答。
- 只有当用户明确要求“总结/介绍/系统讲解”时，才可以使用结构化分段。

风格要求：
- 优先中文回答；用户用英文提问时用英文。
- 保留关键术语、模型名称、公式符号和实验指标。
- 回答要清楚、克制、可验证，不要机械复述片段。

{_MATH_RULES}
"""

_WEB_READING_SYSTEM = f"""
你是一位网页阅读助手。你会基于抓取到的网页正文回答用户的具体问题。

通用原则：
- 只使用提供的网页内容作为依据；网页没有提供的信息要明确说明“当前网页内容无法支持该结论”。
- 多个来源之间有差异时，指出差异并说明来自哪个来源。
- 回答要直接回应用户问题，不要套固定网页总结模板。
- 如果内容涉及近期信息，优先说明网页中能看到的时间、版本或发布信息。
- 保留关键链接、术语、模型名称、代码/API 名称和指标。
- 优先中文回答；用户用英文提问时用英文。

引用方式：
- 需要引用依据时，用“来源 1/来源 2”这样的来源标注，不要编造网页中不存在的出处。
- 如果正文中列出了图片说明，可以把图片作为辅助依据；没有实际图片内容时不要过度解读。

{_MATH_RULES}
"""

_FORMULA_LIBRARY_SYSTEM = f"""
你是科研论文公式讲解助手。用户正在追问同一篇论文中的公式。

必须遵守：
- 只解释提供的论文片段中明确出现的公式、符号和上下文。
- 不要改写成论文总览，不要展开背景、实验、贡献，除非这些内容直接用于解释公式。
- 如果片段只覆盖部分公式，明确说“当前片段只覆盖这些公式”，不要编造其他公式。
- 对每个公式说明：它在优化/模型/实验中的作用、主要符号含义、这一项为什么这样设计。
- 公式用 $...$ 或 $$...$$ 包裹；不要输出裸 LaTeX。

建议结构：
1. 一句话说明这些公式总体在解决什么问题。
2. 按公式编号逐条解释。
3. 总结公式之间的关系。
4. 当前片段未覆盖的信息。

{_MATH_RULES}
"""
_LIBRARY_BASE_SYSTEM = f"""
你是科研论文阅读助手。你会基于提供的论文片段回答用户的具体问题。

通用原则：
- 直接回答用户问的问题，不要套固定论文总览模板。
- 只使用提供的片段作为依据；片段不足时明确说明“当前片段无法支持该结论”。
- 可以解释、重组、归纳片段内容，但不要引入片段之外的具体事实、数据或实验结果。
- 优先中文回答；用户用英文提问时用英文。
- 根据问题类型自然组织答案，标题和段落数量由问题决定。
- 不要为了格式完整而回答用户没问的背景、方法、实验或局限。

{_MATH_RULES}
"""

_TASK_PROMPTS: dict[str, str] = {
    "overview": """
任务类型：论文总览。
回答重点：问题、核心方法、主要结果、当前片段缺口。适合“介绍一下/总结一下/这篇论文讲什么”。
""",
    "formula": """
任务类型：公式讲解。
只解释片段中明确出现的公式、符号和上下文。按公式编号或公式块逐条说明作用、变量含义、每一项为什么存在。不要改写成论文总览。
""",
    "experiment": """
任务类型：实验部分解读。
聚焦实验设置、数据集、指标、baseline、主要结果、消融/对比和实验局限。不要展开无关方法背景。
""",
    "method": """
任务类型：方法部分讲解。
聚焦整体流程、关键模块、设计动机、输入输出、训练目标或推理流程。不要写实验结果，除非用户同时询问。
""",
    "results": """
任务类型：结果分析。
说明片段中报告了哪些结果、支持了什么主张、哪些有数据支撑、哪些只是讨论。
""",
    "limitation": """
任务类型：局限性/不足分析。
区分作者明确承认的局限、从实验设置或方法假设能看出的不足、以及你的合理推断。
""",
    "comparison": """
任务类型：对比分析。
说明对比对象、差异维度、片段依据；片段不足以比较时直接说明。
""",
    "qa": """
任务类型：精确问答。
先给直接答案，再给片段依据；无法回答时说明缺什么信息。不要扩展成完整论文介绍。
""",
}


class ReadingAgent(BaseAgent):
    name = "reading_agent"
    description = "Reads downloaded PDFs directly and answers the user's question."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        input_data = agent_input.input_data
        question: str = input_data.get("question", "What are the key contributions of these papers?")
        mode: str = input_data.get("mode", "direct_pdf")

        if mode == "library_qa":
            return await self._library_qa_generate(agent_input, question, state)

        # ── Direct PDF reading ────────────────────────────────────────────────
        documents: list[dict] = input_data.get("documents", [])
        if not documents:
            documents = state.tool_results.get("literature_download", {}).get("downloaded_pdfs", [])
        if not documents:
            documents = state.working_memory.get("stored_papers", [])

        if not documents:
            return self._error_output(
                agent_input,
                "没有找到可读取的论文。请先搜索并下载论文（例如「搜一下 diffusion model」然后「下载第1篇」）。",
            )

        state.update_stage("paper_reading", self.name)

        from app.storage.factory import get_kv_store
        kv = get_kv_store()

        # Extract text from each PDF (with cache)
        paper_texts: list[tuple[str, str, dict]] = []  # (title, full_text, sections)
        for doc in documents:
            local_path = doc.get("local_path", "")
            title = doc.get("title", "Unknown")
            if not local_path or not os.path.exists(local_path):
                logger.warning("PDF not found: %s", local_path)
                continue
            full_text = ""
            sections: dict = {}
            # Try cache first — KV errors never block extraction
            try:
                pdf_key = _pdf_cache_key(local_path)
                cached = await kv.get(pdf_key)
                if cached:
                    payload = json.loads(cached)
                    full_text = payload["full_text"]
                    sections = payload["sections"]
                    logger.info("Cache HIT pdf '%s': %d chars", title, len(full_text))
            except Exception as cache_exc:
                logger.debug("Cache read failed for '%s': %s", title, cache_exc)
            # Fall back to extraction if cache missed or errored
            if not full_text:
                try:
                    from app.tools.pdf.backends import extract_any
                    result = extract_any(local_path)
                    full_text = result.get("full_text", "")
                    sections = result.get("sections", {})
                    logger.info("Extracted '%s': %d chars", title, len(full_text))
                    if full_text:
                        try:
                            pdf_key = _pdf_cache_key(local_path)
                            await kv.set(pdf_key, json.dumps(
                                {"full_text": full_text, "sections": sections},
                                ensure_ascii=False), ttl=_PDF_TTL)
                        except Exception:
                            pass  # cache write failure is non-fatal
                except Exception as exc:
                    logger.warning("PDF extraction failed for '%s': %s", title, exc)
            if full_text:
                paper_texts.append((title, full_text, sections))

        if not paper_texts:
            return self._error_output(agent_input, "PDF 文本提取失败，请确认文件存在且未损坏。")

        # Build context: one paper or multi-paper
        context_parts: list[str] = []
        titles: list[str] = []
        for title, full_text, sections in paper_texts:
            titles.append(title)
            formatted = _select_content(full_text, sections, question,
                                        budget=_MAX_CHARS // max(len(paper_texts), 1))
            context_parts.append(f"=== {title} ===\n{formatted}")

        context = "\n\n".join(context_parts)
        title_list = "\n".join(f"- {t}" for t in titles)

        # LLM call (cached per paper+question, KV errors never block)
        answer = None
        try:
            if len(documents) == 1:
                qa_key = _qa_cache_key(documents[0].get("local_path", ""), question)
                answer = await kv.get(qa_key)
                if answer:
                    logger.info("Cache HIT qa '%s'", question[:60])
        except Exception:
            qa_key = None

        if not answer:
            try:
                resp = await self.llm.complete(
                    messages=[LLMMessage(
                        role="user",
                        content=f"Papers:\n{title_list}\n\n{context}\n\nQuestion: {question}",
                    )],
                    system=_READING_SYSTEM,
                )
                answer = resp.content
                try:
                    if qa_key:
                        await kv.set(qa_key, answer, ttl=_QA_TTL)
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("LLM call failed: %s", exc)
                answer = "[PDF 文本提取成功但 LLM 调用失败，请检查 LLM 配置]"

        state.update_stage("reading_done", self.name)

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result={
                "reading_notes": [{
                    "title": " | ".join(titles),
                    "question": question,
                    "answer": answer,
                    "contexts": context_parts,
                    "metadata": {
                        "retriever": "direct_pdf",
                        "paper_count": len(titles),
                        "papers": titles,
                    },
                }],
                "question": question,
                "answer": answer,
                "contexts": context_parts,
                "metadata": {
                    "retriever": "direct_pdf",
                    "paper_count": len(titles),
                    "papers": titles,
                },
            },
            next_suggestion="continue_qa_or_dispatch_to_summary_agent",
        )

    async def _library_qa_generate(
        self, agent_input: AgentInput, question: str, state: TaskState
    ) -> AgentOutput:
        """Generate an answer using chunks already retrieved by RetrievalAgent."""
        retrieval_result = state.agent_outputs.get("retrieval_agent", {}).get("result", {})
        using_cached_context = False
        if not retrieval_result:
            retrieval_result = agent_input.input_data.get("cached_library_context", {}) or {}
            using_cached_context = bool(retrieval_result)
        contexts: list[str] = retrieval_result.get("contexts", [])
        paper_list: str = retrieval_result.get("paper_list", "")
        lib_names: list[str] = retrieval_result.get("lib_names", ["知识库"])
        search_question: str = question if using_cached_context else retrieval_result.get("question", question)
        title_filter: str = retrieval_result.get("title_filter", "")
        active_title: str = retrieval_result.get("active_title", "") or title_filter
        question_type = _classify_question_type(question)
        formula_question = question_type == "formula"

        if question_type in {"formula", "experiment", "method", "results", "limitation"} and active_title:
            try:
                from app.rag.long_term.store import get_lt_rag_store
                lt = get_lt_rag_store()
                scoped_chunks = await lt.search_documents(
                    _scoped_retrieval_query(question, question_type),
                    k=10,
                    title_filter=active_title,
                )
                scoped_contexts = [
                    f"[{c.get('metadata', {}).get('title', 'Unknown')} / {c.get('metadata', {}).get('section', '?').upper()}]\n{c.get('document', '')}"
                    for c in scoped_chunks
                    if c.get("document") and c.get("metadata", {}).get("title", "") == active_title
                ]
                if scoped_contexts:
                    contexts = _dedupe_contexts(scoped_contexts + contexts)
                    paper_list = paper_list or active_title
            except Exception as exc:
                logger.debug("%s-scoped retrieval failed for '%s': %s", question_type, active_title, exc)

        if not contexts:
            return self._error_output(
                agent_input,
                "未获取到检索结果，请确认 RetrievalAgent 已成功运行。",
            )

        context = "\n\n---\n\n".join(contexts)
        state.update_stage("paper_reading", self.name)

        try:
            system_prompt = _build_library_system_prompt(question_type)
            resp = await self.llm.complete(
                messages=[LLMMessage(
                    role="user",
                    content=(
                        f"Papers in your personal library:\n{paper_list}\n\n"
                        f"Retrieved excerpts:\n{context}\n\n"
                        f"Question: {search_question}"
                    ),
                )],
                system=system_prompt,
            )
            answer = resp.content
        except Exception as exc:
            logger.exception("LLM call failed in _library_qa_generate: %s", exc)
            answer = "[知识库检索成功但 LLM 调用失败]"

        state.update_stage("reading_done", self.name)

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result={
                "reading_notes": [{
                    "title": active_title or f"[{' / '.join(lib_names)}]",
                    "question": question,
                    "answer": answer,
                    "contexts": contexts,
                    "metadata": {
                        "retriever": "long_term_library",
                        "libraries": lib_names,
                        "chunk_count": len(contexts),
                        "active_title": active_title,
                        "cached_context": using_cached_context,
                        "question_type": question_type,
                        "title_filter": title_filter,
                    },
                }],
                "question": search_question,
                "answer": answer,
                "contexts": contexts,
                "metadata": {
                    "retriever": "long_term_library",
                    "libraries": lib_names,
                    "chunk_count": len(contexts),
                    "active_title": active_title,
                    "cached_context": using_cached_context,
                    "question_type": question_type,
                    "title_filter": title_filter,
                },
            },
            artifacts={"source": "library"},
            next_suggestion="continue_qa_or_dispatch_to_summary_agent",
        )

    def _error_output(self, agent_input: AgentInput, message: str) -> AgentOutput:
        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.FAILED,
            errors=[message],
        )

    async def _web_read_generate(
        self, agent_input: AgentInput, question: str, state: TaskState
    ) -> AgentOutput:
        """Fetch web pages, merge cleaned content, and answer from page text."""
        urls = [str(u).strip() for u in agent_input.input_data.get("urls", []) if str(u).strip()]
        search_results: list[dict] = []

        if not urls:
            urls = extract_urls(question)

        if not urls:
            try:
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
            [_scrape_web_page_cached(item["url"], question, bypass_cache) for item in targets],
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
            logger.exception("LLM call failed in _web_read_generate: %s", exc)
            answer = "[网页抓取成功但 LLM 调用失败，请检查 LLM 配置]"

        state.update_stage("reading_done", self.name)
        metadata = {
            "retriever": "web_read",
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
                "reading_notes": [{
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
            next_suggestion="continue_qa_or_dispatch_to_summary_agent",
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _scrape_web_page_cached(url: str, question: str, bypass_cache: bool) -> dict:
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
    """Tokenize a question into matchable terms for both English and Chinese.

    English: keep words of 3+ characters.
    Chinese: extract all 2-character bigrams (Chinese has no word spaces).
    """
    english = {w for w in re.findall(r"[a-zA-Z]{3,}", text.lower())}
    chinese_spans = re.findall(r"[一-鿿]+", text)
    bigrams: set[str] = set()
    for span in chinese_spans:
        if len(span) == 1:
            bigrams.add(span)
        else:
            bigrams.update(span[i:i + 2] for i in range(len(span) - 1))
    return english | bigrams


def _is_formula_question(question: str) -> bool:
    return _classify_question_type(question) == "formula"


def _classify_question_type(question: str) -> str:
    q = question.lower()
    checks: list[tuple[str, list[str]]] = [
        ("formula", ["公式", "方程", "损失函数", "目标函数", "符号", "推导",
                     "formula", "equation", "loss function", "objective function", "math", "symbol", "notation"]),
        ("experiment", ["实验", "实验设置", "数据集", "指标", "baseline", "消融", "ablation",
                        "experiment", "evaluation", "dataset", "metric", "benchmark"]),
        ("results", ["结果", "性能", "效果", "提升", "表格", "table", "result", "performance", "improvement"]),
        ("method", ["方法", "模型", "架构", "模块", "算法", "流程", "训练", "推理",
                    "method", "model", "architecture", "module", "algorithm", "pipeline", "training", "inference"]),
        ("limitation", ["局限", "不足", "缺点", "问题", "失败", "limitation", "weakness", "drawback", "failure"]),
        ("comparison", ["对比", "区别", "相比", "差异", "compare", "comparison", "difference", "versus", " vs "]),
        ("overview", ["介绍", "总结", "概述", "讲什么", "贡献", "overview", "summary", "summarize", "contribution"]),
    ]
    for kind, markers in checks:
        if any(marker in q for marker in markers):
            return kind
    return "qa"


def _build_library_system_prompt(question_type: str) -> str:
    return _LIBRARY_BASE_SYSTEM + "\n\n" + _TASK_PROMPTS.get(question_type, _TASK_PROMPTS["qa"])


def _scoped_retrieval_query(question: str, question_type: str) -> str:
    expansions = {
        "formula": "formula equation loss objective function symbol notation",
        "experiment": "experiment evaluation dataset metric baseline ablation implementation result",
        "method": "method model architecture module algorithm pipeline training inference",
        "results": "result performance table comparison improvement metric",
        "limitation": "limitation weakness failure discussion future work",
    }
    return f"{question} {expansions.get(question_type, '')}".strip()


def _dedupe_contexts(contexts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ctx in contexts:
        key = ctx[:500]
        if key in seen:
            continue
        seen.add(key)
        result.append(ctx)
    return result


# Sections that are most likely to contain methods / algorithm details.
# Used as a tiebreaker when keyword scores are all zero (e.g. purely Chinese query).
_METHOD_SECTION_PRIORITY: dict[str, int] = {
    "method": 10, "methods": 10, "methodology": 10,
    "approach": 9, "model": 9, "algorithm": 10,
    "framework": 8, "architecture": 8, "proposed": 8,
    "technique": 7, "training": 7, "implementation": 6,
    "experiment": 5, "experiments": 5, "evaluation": 5, "results": 4,
    "analysis": 3, "discussion": 3,
    "related": 2, "background": 2,
    "conclusion": 1, "conclusions": 1,
}


def _section_priority(name: str) -> int:
    key = name.lower().strip()
    for k, v in _METHOD_SECTION_PRIORITY.items():
        if k in key:
            return v
    return 0


def _select_content(full_text: str, sections: dict, question: str, budget: int) -> str:
    """Return the most relevant text within budget characters.

    Strategy:
    1. Always include abstract + introduction (grounding).
    2. Score remaining sections by keyword overlap with the question.
       Handles both English (word tokens) and Chinese (character bigrams).
    3. When all keyword scores are 0 (e.g. a Chinese query against an English paper),
       fall back to a section-name priority list so method/algorithm sections
       are preferred over unrelated ones.
    4. Fill remaining budget with top-scored sections.
    """
    if len(full_text) <= budget:
        return full_text

    q_tokens = _q_tokens(question)

    # Always-include sections
    priority_keys = ["abstract", "introduction"]
    result_parts: list[str] = []
    used = 0
    included = set()

    for key in priority_keys:
        if key in sections:
            chunk = f"[{key.upper()}]\n{sections[key]}"
            result_parts.append(chunk)
            used += len(chunk)
            included.add(key)

    # Score the rest by keyword overlap, then section-name priority as tiebreaker
    skip = {"preamble", "references"} | included
    scored: list[tuple[int, int, str, str]] = []
    for name, text in sections.items():
        if name in skip:
            continue
        section_tokens = _q_tokens(text)
        kw_score = len(q_tokens & section_tokens) if q_tokens else 0
        name_score = _section_priority(name)
        scored.append((kw_score, name_score, name, text))
    scored.sort(reverse=True)

    for _, _, name, text in scored:
        chunk = f"[{name.upper()}]\n{text}"
        if used + len(chunk) <= budget:
            result_parts.append(chunk)
            used += len(chunk)
        else:
            remaining = budget - used
            if remaining > 200:
                result_parts.append(f"[{name.upper()}]\n{text[:remaining]}…")
            break

    return "\n\n---\n\n".join(result_parts)
