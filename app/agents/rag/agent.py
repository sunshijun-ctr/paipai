"""RagAgent — unified retrieval + reading.

Replaces the retrieval_agent → reading_agent pair in the workflow graphs.
The two old agents always ran in lock-step; this agent collapses them into
one workflow node while preserving both intermediate outputs in
`state.agent_outputs` so existing downstream consumers (e.g. the writing
workflow's `_collect_library_material_node`) still find what they expect.

Mode dispatch is internal:
  * library_qa / library  → retrieve from long-term library → read with chunks
  * uploaded_file         → skip retrieval, read directly from stored uploads
  * web                   → skip retrieval, read from web context

The retrieval-failure tolerance previously hard-coded inside `make_agent_node`
(continue writing if retrieval fails but uploads exist) is replicated here so
behaviour is unchanged after the merge.
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.state.task_state import TaskState
from app.services.llm import BaseLLMProvider, LLMMessage
import hashlib
from app.rag.long_term.store import get_lt_rag_store
from app.storage.factory import get_kv_store
import os
import logging
import json

logger = logging.getLogger(__name__)


def _needs_retrieval(state: TaskState) -> bool:
    """Library mode (or implicit library mode with no uploads) needs retrieval.

    Direct uploaded-file / web reading does not."""
    wm = state.working_memory
    qa_source = (wm.get("qa_source") or "").strip().lower()
    if qa_source in {"uploaded_file", "web"}:
        return False
    if wm.get("web_read_mode"):
        return False
    if wm.get("library_qa_mode"):
        return True
    # For academic_writing the writing-source mode tells us
    writing_source = (wm.get("writing_source") or "").strip().lower()
    if writing_source in {"library", "both", "upload_plus_library"}:
        return True
    # Fall-back: if there's no uploaded material at hand, retrieval is the
    # only way to ground the answer.
    return not bool(wm.get("stored_papers"))


def _has_uploaded_writing_docs(state: TaskState) -> bool:
    import os
    return any(
        p.get("source") == "upload" and p.get("local_path") and os.path.exists(p.get("local_path", ""))
        for p in state.working_memory.get("stored_papers", [])
    )


def _is_writing_with_uploads(state: TaskState) -> bool:
    """Match the legacy retrieval-failure tolerance: writing workflow with
    uploaded materials can proceed without library retrieval."""
    wm = state.working_memory
    writing_in_flight = (
        bool(wm.get("writing_source"))
        or bool(wm.get("uploaded_writing_docs"))
        or wm.get("writing_followup", False)
    )
    return writing_in_flight and _has_uploaded_writing_docs(state)


class RagAgent(BaseAgent):
    name = "rag_agent"
    description = (
        "Unified RAG agent: optionally retrieves chunks from the personal "
        "library, then reads / synthesises an answer. Replaces the legacy "
        "retrieval_agent + reading_agent pair."
    )

    def __init__(self, llm: BaseLLMProvider) -> None:
        # single llm provider used for reading-phase LLM calls
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        # Phase 1: retrieval (inline, simplified)
        retrieval_result: dict = {}
        if _needs_retrieval(state):
            question = agent_input.input_data.get("question", agent_input.user_goal)
            try:
                lt = get_lt_rag_store()
                chunks = await lt.search_documents(question or agent_input.user_goal, k=8)
            except Exception as exc:
                return AgentOutput(
                    task_id=agent_input.task_id,
                    session_id=agent_input.session_id,
                    agent_name=self.name,
                    status=AgentStatus.FAILED,
                    result={},
                    errors=[f"知识库检索失败: {exc}"],
                )

            if not chunks:
                return AgentOutput(
                    task_id=agent_input.task_id,
                    session_id=agent_input.session_id,
                    agent_name=self.name,
                    status=AgentStatus.FAILED,
                    result={},
                    errors=["知识库中未找到与问题相关的内容。"],
                )

            contexts = [
                f"[{c.get('metadata', {}).get('title', 'Unknown')} / {c.get('metadata', {}).get('section', '?').upper()}]\n{c.get('document', '')}"
                for c in chunks if c.get('document')
            ]
            active_title = chunks[0].get('metadata', {}).get('title', '') if chunks else ""
            retrieval_result = {
                "retrieved_chunks": chunks,
                "contexts": contexts,
                "question": question,
                "original_question": question,
                "active_title": active_title,
                "metadata": {
                    "retriever": "long_term_library",
                    "chunk_count": len(chunks),
                },
            }
            state.record_agent_output("retrieval_agent", retrieval_result)

        # Phase 2: reading (inline, supports library_qa and direct_pdf)
        mode = "library_qa" if state.working_memory.get("library_qa_mode") else "direct_pdf"
        if mode == "library_qa":
            retrieval = retrieval_result or state.agent_outputs.get("retrieval_agent", {}).get("result", {})
            contexts = retrieval.get("contexts", [])
            if not contexts:
                return AgentOutput(
                    task_id=agent_input.task_id,
                    session_id=agent_input.session_id,
                    agent_name=self.name,
                    status=AgentStatus.FAILED,
                    result={},
                    errors=["未获取到检索结果，请确认检索成功。"],
                )
            context = "\n\n---\n\n".join(contexts)
            system_prompt = _LIBRARY_SYSTEM
            try:
                resp = await self.llm.complete(
                    messages=[LLMMessage(role="user", content=(f"Retrieved excerpts:\n{context}\n\nQuestion: {agent_input.user_goal}"))],
                    system=system_prompt,
                )
                answer = resp.content
            except Exception as exc:
                logging.exception("LLM call failed in rag_agent reading: %s", exc)
                answer = "[知识库检索成功但 LLM 调用失败]"

            reading_result = {
                "reading_notes": [{
                    "title": retrieval.get("active_title", ""),
                    "question": agent_input.user_goal,
                    "answer": answer,
                    "contexts": contexts,
                    "metadata": {"retriever": "long_term_library"},
                }],
                "question": agent_input.user_goal,
                "answer": answer,
                "contexts": contexts,
            }
        else:
            # direct_pdf: use stored_papers or tool_results
            documents = agent_input.input_data.get("documents") or state.tool_results.get("literature_download", {}).get("downloaded_pdfs", []) or state.working_memory.get("stored_papers", [])
            if not documents:
                return AgentOutput(
                    task_id=agent_input.task_id,
                    session_id=agent_input.session_id,
                    agent_name=self.name,
                    status=AgentStatus.FAILED,
                    result={},
                    errors=["没有找到可读取的论文。"],
                )
            # Simple extraction: try to load cached full_text from KV store
            kv = get_kv_store()
            paper_texts = []
            for doc in documents:
                local_path = doc.get("local_path", "")
                title = doc.get("title", "Unknown")
                full_text = ""
                sections = {}
                if local_path and os.path.exists(local_path):
                    try:
                        pdf_key = _pdf_cache_key(local_path)
                        cached = await kv.get(pdf_key)
                        if cached:
                            payload = json.loads(cached)
                            full_text = payload.get("full_text", "")
                            sections = payload.get("sections", {})
                    except Exception:
                        pass
                if not full_text:
                    try:
                        from app.tools.pdf.backends import extract_any
                        result = extract_any(local_path)
                        full_text = result.get("full_text", "")
                        sections = result.get("sections", {})
                        if full_text:
                            try:
                                pdf_key = _pdf_cache_key(local_path)
                                await kv.set(pdf_key, json.dumps({"full_text": full_text, "sections": sections}, ensure_ascii=False), ttl=_PDF_TTL)
                            except Exception:
                                pass
                    except Exception as exc:
                        logging.warning("PDF extraction failed for %s: %s", title, exc)
                if full_text:
                    paper_texts.append((title, full_text, sections))

            if not paper_texts:
                return AgentOutput(
                    task_id=agent_input.task_id,
                    session_id=agent_input.session_id,
                    agent_name=self.name,
                    status=AgentStatus.FAILED,
                    result={},
                    errors=["PDF 文本提取失败，请确认文件存在且未损坏。"],
                )

            context_parts = []
            titles = []
            for title, full_text, sections in paper_texts:
                titles.append(title)
                budget = _MAX_CHARS // max(len(paper_texts), 1)
                formatted = full_text[:budget]
                context_parts.append(f"=== {title} ===\n{formatted}")
            context = "\n\n".join(context_parts)
            title_list = "\n".join(f"- {t}" for t in titles)
            try:
                resp = await self.llm.complete(
                    messages=[LLMMessage(role="user", content=f"Papers:\n{title_list}\n\n{context}\n\nQuestion: {agent_input.user_goal}" )],
                    system=_READING_SYSTEM,
                )
                answer = resp.content
            except Exception as exc:
                logging.exception("LLM call failed: %s", exc)
                answer = "[PDF 文本提取成功但 LLM 调用失败，请检查 LLM 配置]"

            reading_result = {
                "reading_notes": [{
                    "title": " | ".join(titles),
                    "question": agent_input.user_goal,
                    "answer": answer,
                    "contexts": context_parts,
                    "metadata": {"retriever": "direct_pdf", "paper_count": len(titles), "papers": titles},
                }],
                "question": agent_input.user_goal,
                "answer": answer,
                "contexts": context_parts,
            }

        state.record_agent_output("reading_agent", reading_result)

        merged_result = {**retrieval_result, **reading_result}
        merged_result["_phases"] = {"retrieved": bool(retrieval_result), "retrieval_status": retrieval_result.get("metadata", {}).get("status") if retrieval_result else "skipped"}

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result=merged_result,
        )

    def _make_subagent_input(self, parent: AgentInput, agent_name: str) -> AgentInput:
        return AgentInput(
            task_id=parent.task_id,
            session_id=parent.session_id,
            agent_name=agent_name,
            user_goal=parent.user_goal,
            current_stage=parent.current_stage,
            input_data=parent.input_data,
            context=parent.context,
        )

# --- Inline small helpers/constants borrowed from reading agent
_QA_TTL = 1800
_PDF_TTL = 24 * 3600

def _pdf_cache_key(local_path: str) -> str:
    mtime = int(os.path.getmtime(local_path))
    tag = hashlib.md5(local_path.encode()).hexdigest()[:12]
    return f"ra:pdf:{tag}:{mtime}"

_MAX_CHARS = 50_000

# Minimal system prompts reused from reading agent
_READING_SYSTEM = "你是一位科研论文阅读助手。你会基于提供的论文原文回答用户的具体问题。"
_LIBRARY_SYSTEM = "你是一位科研论文阅读助手。你会基于检索到的论文片段回答用户的具体问题。"
