import logging
import os
import re
from dataclasses import dataclass

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)

# ── RetrievalAgent ─────────────────────────────────────────────────────────
# 职责：检索知识库并评估检索质量（不生成内容）
# - 调用 LongTermRAGStore 搜索相关文档chunks
# - 调用 EvaluationService 评估检索的context_precision
# - LLM 调用发生在 EvaluationService 内部（有独立的评估提示词）
# - 不定义 system prompt（本agent不生成内容）
# ───────────────────────────────────────────────────────────────────────────


class RetrievalAgent(BaseAgent):
    name = "retrieval_agent"
    description = "Retrieves relevant chunks from the personal library and evaluates retrieval quality (context_precision)."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        input_data = agent_input.input_data
        question: str = input_data.get("question", agent_input.user_goal)

        from app.rag.long_term.store import get_lt_rag_store
        lt = get_lt_rag_store()
        all_docs = await lt.list_all_documents()
        total_titles = [t for titles in all_docs.values() for t in titles]
        document_records = await _list_all_document_records(lt)

        if not total_titles:
            return self._error_output(
                agent_input,
                "当前会话没有已下载的论文，知识库也是空的。\n"
                "请先：① 搜索并下载论文（「搜一下 xxx」然后「下载第1篇」），"
                "或 ② 在右侧面板上传文档并加入知识库，再重新提问。",
            )

        state.update_stage("retrieval", self.name)

        target = _resolve_library_target(question, document_records)
        if target.requested and not target.title:
            requested = target.requested_text or "目标文献"
            return self._error_output(
                agent_input,
                f"知识库中没有找到目标文献：{requested}。请确认文献已经加入知识库，或使用知识库中显示的论文标题/上传文件别名提问。",
            )

        title_filter = target.title or _extract_library_title(question)
        search_question = target.search_question or _strip_library_scope(question)
        lib_ids = [target.lib_id] if target.lib_id else None

        try:
            if _looks_like_library_overview(question) and not title_filter:
                search_question = _library_overview_query(question)
                chunks = await _retrieve_library_overview(lt, all_docs, k=10)
                if not chunks:
                    chunks = await lt.search_documents(search_question, k=10)
            else:
                chunks = await lt.search_documents(
                    search_question or question,
                    k=8,
                    lib_ids=lib_ids,
                    title_filter=title_filter,
                )
        except Exception as exc:
            return self._error_output(agent_input, f"知识库检索失败: {exc}")

        if not chunks and title_filter:
            return self._error_output(
                agent_input,
                f"已限定到目标文献《{title_filter}》，但没有检索到可用于回答该问题的相关片段；已按单篇约束停止，不回退到全库检索。",
            )

        if not chunks:
            return self._error_output(
                agent_input,
                "知识库中未找到与问题相关的内容。"
                "如果你想针对已下载的 PDF 提问，请先确认该 PDF 已在当前会话中下载（右侧面板可查看）。",
            )

        contexts = [
            f"[{c.get('metadata', {}).get('title', 'Unknown')} / {c.get('metadata', {}).get('section', '?').upper()}]\n{c.get('document', '')}"
            for c in chunks
            if c.get("document")
        ]
        active_title = chunks[0].get("metadata", {}).get("title", "") if chunks else ""

        # Evaluate retrieval quality: context_precision only (no answer available yet)
        retrieval_eval: dict | None = None
        try:
            from app.services.evaluation import EvaluationService
            svc = EvaluationService()
            retrieval_eval = await svc.evaluate_retrieval(
                question=search_question or question,
                contexts=contexts,
            )
        except Exception as exc:
            logger.warning("Retrieval evaluation failed: %s", exc)

        # Build a human-readable library overview for ReadingAgent
        libs = lt.list_libraries()
        if title_filter:
            paper_list = title_filter
        else:
            lib_lines = []
            for lib in libs:
                doc_titles = all_docs.get(lib["lib_id"], [])
                if doc_titles:
                    lib_lines.append(f"[{lib['name']}] " + ", ".join(doc_titles[:5]))
            paper_list = "\n".join(lib_lines) or "\n".join(f"- {t}" for t in total_titles[:10])

        lib_names = list(dict.fromkeys(
            c.get("metadata", {}).get("lib_name", "知识库")
            for c in chunks
        ))

        state.update_stage("retrieval_done", self.name)

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result={
                "retrieved_chunks": chunks,
                "contexts": contexts,
                "question": search_question or question,
                "original_question": question,
                "paper_list": paper_list,
                "lib_names": lib_names,
                "active_title": active_title,
                "title_filter": title_filter,
                "retrieval_eval": retrieval_eval,
                "metadata": {
                    "retriever": "long_term_library",
                    "libraries": lib_names,
                    "chunk_count": len(chunks),
                    "active_title": active_title,
                    "title_filter": title_filter,
                },
            },
            next_suggestion="pass_to_reading_agent",
        )

    def _error_output(self, agent_input: AgentInput, message: str) -> AgentOutput:
        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.FAILED,
            errors=[message],
        )


# ── Helpers (mirrored from reading agent, now owned by retrieval) ─────────────

@dataclass
class _LibraryTarget:
    requested: bool = False
    requested_text: str = ""
    title: str = ""
    lib_id: str = ""
    search_question: str = ""


async def _list_all_document_records(lt) -> list[dict]:
    records: list[dict] = []
    for lib in lt.list_libraries():
        lib_id = lib["lib_id"]
        try:
            for record in await lt.list_document_records(lib_id):
                records.append({**record, "lib_id": record.get("lib_id") or lib_id})
        except Exception as exc:
            logger.debug("list_document_records failed for %s: %s", lib_id, exc)
    return records


def _resolve_library_target(question: str, records: list[dict]) -> _LibraryTarget:
    q_norm = _normalize_target_text(question)
    best: tuple[int, dict, str] | None = None
    for record in records:
        for alias in _document_aliases(record):
            alias_norm = _normalize_target_text(alias)
            if not alias_norm:
                continue
            if alias_norm in q_norm:
                score = len(alias_norm)
                if best is None or score > best[0]:
                    best = (score, record, alias)

    explicit_mentions = _explicit_document_mentions(question)
    if best:
        _, record, alias = best
        return _LibraryTarget(
            requested=True,
            requested_text=alias,
            title=record.get("title", ""),
            lib_id=record.get("lib_id", ""),
            search_question=_strip_target_from_question(question, alias),
        )
    if explicit_mentions:
        return _LibraryTarget(
            requested=True,
            requested_text=explicit_mentions[0],
            search_question=_strip_target_from_question(question, explicit_mentions[0]),
        )
    return _LibraryTarget(search_question=_strip_library_scope(question))


def _document_aliases(record: dict) -> list[str]:
    aliases: list[str] = []
    title = str(record.get("title") or "").strip()
    source = str(record.get("source") or "").strip()
    source_name = os.path.basename(source)
    source_stem = os.path.splitext(source_name)[0]
    stripped_source_name = re.sub(r"^[0-9a-fA-F]{6,}[_-]", "", source_name)
    stripped_source_stem = os.path.splitext(stripped_source_name)[0]
    for value in [title, source_name, source_stem, stripped_source_name, stripped_source_stem]:
        value = value.strip()
        if value and value not in aliases:
            aliases.append(value)
    if title.lower().endswith(".pdf"):
        stem = os.path.splitext(title)[0]
        if stem and stem not in aliases:
            aliases.append(stem)
    return aliases


def _explicit_document_mentions(question: str) -> list[str]:
    mentions: list[str] = []
    quoted_patterns = [
        r"[《「『“\"]([^《》「」『』“”\"]+?)[》」』”\"]",
        r"'([^']+?)'",
    ]
    for pattern in quoted_patterns:
        for match in re.finditer(pattern, question):
            value = match.group(1).strip()
            if _looks_like_document_name(value):
                mentions.append(value)

    for match in re.finditer(r"([^\s，,。；;：:?？《》「」『』“”\"']+\.(?:pdf|pptx|txt|md|docx?))", question, flags=re.I):
        mentions.append(match.group(1).strip())

    scoped_patterns = [
        r"(?:只看|只对|针对|关于|阅读|查询|检索|分析|总结)\s*([^，,。；;：:?？\n]+)",
        r"(?:paper|file|document)\s+([A-Za-z0-9_.+\- ]{3,80})",
    ]
    for pattern in scoped_patterns:
        for match in re.finditer(pattern, question, flags=re.I):
            value = _trim_mention(match.group(1))
            if _looks_like_file_alias(value):
                mentions.append(value)

    deduped: list[str] = []
    for item in mentions:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _looks_like_document_name(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if re.search(r"\.(pdf|pptx|txt|md|docx?)$", value, flags=re.I):
        return True
    return len(value) >= 3 and not any(token in value for token in ["知识库", "文献库", "资料库"])


def _looks_like_file_alias(text: str) -> bool:
    return bool(re.search(r"\.(pdf|pptx|txt|md|docx?)$", text.strip(), flags=re.I))


def _trim_mention(text: str) -> str:
    value = text.strip()
    value = re.sub(r"(这篇|这篇论文|这篇文章|这篇文献|论文|文章|文献|文件)$", "", value).strip()
    value = re.sub(r"(的方法|的公式|的实验|的结果|讲了什么|是什么)$", "", value).strip()
    return value.strip("：:，,。；;？? ")


def _strip_target_from_question(question: str, target: str) -> str:
    text = _strip_library_scope(question)
    if target:
        text = re.sub(re.escape(target), "", text, flags=re.I)
    text = re.sub(r"(只看|只对|针对|关于|阅读|查询|检索|分析|总结)", "", text)
    text = re.sub(r"(这篇|这篇论文|这篇文章|这篇文献|论文|文章|文献|文件)", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:，,。；;？?")
    return text or question


def _normalize_target_text(text: str) -> str:
    value = (text or "").lower()
    value = value.replace("\\", "/")
    value = os.path.basename(value)
    value = re.sub(r"[\s_\-–—]+", "", value)
    value = re.sub(r"[《》「」『』“”\"'`.,，。:：;；?？()\[\]{}]", "", value)
    return value


def _extract_library_title(question: str) -> str:
    for pattern in [
        r"[《「『](.+?)[》」』]",
        r"from\s+(?:my\s+)?library\s+(.+?)(?:,|:|\?|$)",
    ]:
        match = re.search(pattern, question, flags=re.I)
        if match:
            return match.group(1).strip()
    return ""


def _strip_library_scope(question: str) -> str:
    text = re.sub(
        r"^\s*(请)?\s*(从|在)?\s*(我的|个人)?\s*(知识库|文献库|资料库|长期库)(中|里)?\s*[，,:：]?\s*",
        "",
        question,
    )
    text = re.sub(r"关于[《「『].+?[》」』]\s*[，,:：]?\s*", "", text)
    text = re.sub(r"针对[《「『].+?[》」』]\s*[，,:：]?\s*", "", text)
    return text.strip()


def _looks_like_library_overview(question: str) -> bool:
    q = question.lower()
    return any(marker in q for marker in [
        "总结一下我的知识库",
        "总结我的知识库",
        "知识库中目前重要的知识",
        "知识库目前重要的知识",
        "知识库里重要的知识",
        "知识库概览",
        "文献库概览",
        "summarize my knowledge base",
        "knowledge base overview",
        "library overview",
    ])


def _library_overview_query(question: str) -> str:
    if any(token in question for token in ["图像", "HDR", "去噪", "视觉", "成像"]):
        return "abstract introduction contribution method conclusion image processing imaging denoising HDR"
    return "abstract introduction contribution method conclusion key findings"


async def _retrieve_library_overview(lt, all_docs: dict[str, list[str]], k: int = 10) -> list[dict]:
    """Collect representative chunks per document for broad library summary requests."""
    overview_query = "abstract introduction contribution method conclusion key findings"
    chunks: list[dict] = []
    seen_ids: set[str] = set()
    titles: list[str] = []
    for doc_titles in all_docs.values():
        for title in doc_titles:
            if title not in titles:
                titles.append(title)

    for title in titles[:12]:
        try:
            hits = await lt.search_documents(overview_query, k=3, title_filter=title)
        except Exception as exc:
            logger.debug("overview retrieval failed for %s: %s", title, exc)
            continue
        for hit in hits:
            chunk_id = str(hit.get("id") or hit.get("chunk_id") or "")
            text = str(hit.get("document") or "")
            if not text.strip() or _is_low_value_library_chunk(text):
                continue
            if chunk_id and chunk_id in seen_ids:
                continue
            if chunk_id:
                seen_ids.add(chunk_id)
            chunks.append(hit)
            break
        if len(chunks) >= k:
            break
    return chunks


def _is_low_value_library_chunk(text: str) -> bool:
    lowered = text.lower()
    low_value_markers = [
        "downloaded from",
        "authorized licensed use",
        "ieee xplore",
        "all rights reserved",
        "copyright",
        "terms of use",
        "download count",
        "last updated",
    ]
    if any(marker in lowered for marker in low_value_markers):
        return True
    return len(text.strip()) < 180
