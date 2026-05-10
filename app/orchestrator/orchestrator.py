import logging
import re
import uuid
from typing import Awaitable, Callable, Optional

from app.agents.base.agent import BaseAgent
from app.config.settings import settings
from app.agents.chat.agent import ChatAgent
from app.agents.general.agent import GeneralAgent
from app.agents.intent.agent import IntentAgent
from app.agents.literature.agent import LiteratureAgent
from app.agents.note.agent import NoteAgent
from app.agents.reading.agent import ReadingAgent
from app.agents.retrieval.agent import RetrievalAgent
from app.agents.summary.agent import SummaryAgent
from app.agents.web.agent import WebAgent
from app.agents.writing.agent import WritingAgent

from app.memory.manager import MemoryManager
from app.schemas.agent import AgentInput, AgentStatus
from app.services.llm import BaseLLMProvider, get_agent_llm_providers, get_llm_provider
from app.session.context import SessionContext
from app.state.task_state import TaskState

from app.tools.base import ToolRegistry
from app.tools.download.tool import DownloadTool
from app.tools.filter.tool import FilterTool
from app.tools.image.analyze_image_tool import AnalyzeImageTool
from app.tools.library.add_tool import AddToLibraryTool
from app.tools.pdf.tool import LlamaIndexTool
from app.tools.search.tool import SearchTool
from app.tools.web import UrlFetchTool, WebScrapeTool, WebSearchTool
from app.orchestrator.pending_action_handler import handle_pending_action
from app.orchestrator.task_context import (
    apply_task_context_to_state,
    build_contextual_intent_query,
    continuation_workflow,
    detect_task_relation,
    repair_task_context_from_history,
)
from app.orchestrator.router import (
    extract_writing_source,
    has_uploaded_writing_docs,
    looks_like_library_query,
    looks_like_web_read_query,
    looks_like_writing_followup,
    paper_search_intent_from_query,
    resolve_workflow,
    should_use_cached_library_context,
    workflow_agent_plan,
    workflow_intent_label_key,
)
from app.workflows.image_understanding_graph import has_image_path_marker
from app.workflows.executor import run_agent_workflow

logger = logging.getLogger(__name__)

# Stage -> agent name mapping
_STAGE_AGENT_MAP: dict[str, str] = {
    "literature_search": "literature_agent",
    "literature_filter": "literature_agent",
    "pdf_download": "literature_agent",
    "retrieval": "retrieval_agent",
    "retrieval_done": "retrieval_agent",
    "paper_writing": "writing_agent",
    "paper_reading": "reading_agent",
    "web_search": "web_agent",
    "web_fetch": "web_agent",
    "web_reading": "web_agent",
    "web_done": "web_agent",
    "final_summary": "summary_agent",
    "general_chat": "chat_agent",
}


class Orchestrator:
    def __init__(self, llm: Optional[BaseLLMProvider] = None) -> None:
        self.llm = llm or get_llm_provider()
        self._agent_llms: dict[str, BaseLLMProvider] = {}
        self._agents: dict[str, BaseAgent] = {}
        self._session_contexts: dict[str, SessionContext] = {}
        self._setup_tools()
        self._setup_agents()

    def _setup_tools(self) -> None:
        image_tool = AnalyzeImageTool()
        for tool in [
            SearchTool(),
            FilterTool(),
            DownloadTool(),
            LlamaIndexTool(),
            AddToLibraryTool(),
            WebSearchTool(),
            UrlFetchTool(),
            WebScrapeTool(),
            image_tool,
        ]:
            ToolRegistry.register(tool)
        image_tool.preheat_ocr()
        logger.info("Registered tools: %s", ToolRegistry.list_names())

    def _setup_agents(self) -> None:
        self._agent_llms = get_agent_llm_providers()
        fallback = self.llm
        self._agents = {
            "intent_agent": IntentAgent(self._agent_llms.get("intent_agent", fallback)),
            "general_agent": GeneralAgent(self._agent_llms.get("general_agent", self._agent_llms.get("chat_agent", fallback))),
            "literature_agent": LiteratureAgent(self._agent_llms.get("literature_agent", fallback)),
            "retrieval_agent": RetrievalAgent(self._agent_llms.get("retrieval_agent", fallback)),
            "reading_agent": ReadingAgent(self._agent_llms.get("reading_agent", fallback)),
            "web_agent": WebAgent(self._agent_llms.get("web_agent", self._agent_llms.get("reading_agent", fallback))),
            "writing_agent": WritingAgent(self._agent_llms.get("writing_agent", fallback)),
            "note_agent": NoteAgent(self._agent_llms.get("note_agent", fallback)),
            "summary_agent": SummaryAgent(self._agent_llms.get("summary_agent", fallback)),
            "chat_agent": ChatAgent(self._agent_llms.get("chat_agent", fallback)),
        }
        self.llm = self._agent_llms.get("chat_agent", fallback)
        logger.info("Registered agents: %s", list(self._agents))

    def reload_llm_config(self) -> None:
        """Rebuild agents with the latest per-agent LLM settings."""
        self._setup_agents()
        logger.info("Reloaded per-agent LLM configuration")

    async def run(
        self,
        user_query: str,
        session_id: Optional[str] = None,
        conversation_history: Optional[list] = None,
        stored_papers: Optional[list] = None,
        found_papers: Optional[list] = None,
        memory_manager: Optional[MemoryManager] = None,
        progress_callback: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> TaskState:
        async def progress(step: str, text: str, pct: int | None = None) -> None:
            if progress_callback:
                await progress_callback({"step": step, "text": text, "pct": pct})

        effective_session_id = session_id or f"session_{uuid.uuid4().hex[:8]}"
        session_context = self._load_session_context(effective_session_id, memory_manager)
        if conversation_history is not None:
            session_context.recent_turns = conversation_history

        state = TaskState(
            user_goal=user_query,
            session_id=effective_session_id,
        )
        state.working_memory["conversation_history"] = conversation_history or session_context.recent_turns
        state.working_memory["session_context"] = session_context.to_dict()
        if session_context.current_task:
            state.working_memory["current_task"] = session_context.current_task
        if session_context.history_summary:
            state.working_memory["history_summary"] = session_context.history_summary
        if stored_papers:
            state.working_memory["stored_papers"] = stored_papers
        if found_papers:
            state.working_memory["found_papers"] = found_papers
        active_task_context = memory_manager.short_term.active_task_context if memory_manager else {}
        task_relation = detect_task_relation(user_query, active_task_context)
        if memory_manager and task_relation == "continue_task":
            active_task_context = repair_task_context_from_history(
                active_task_context,
                memory_manager.short_term.recent_turns,
            )
        apply_task_context_to_state(state, active_task_context, task_relation)
        if memory_manager and memory_manager.short_term.current_library_context:
            state.working_memory["current_library_context"] = memory_manager.short_term.current_library_context
        if memory_manager and memory_manager.short_term.current_writing_context and task_relation != "continue_task":
            state.working_memory["current_writing_context"] = memory_manager.short_term.current_writing_context
        if memory_manager and memory_manager.short_term.history_summary:
            state.working_memory["history_summary"] = memory_manager.short_term.history_summary
        if memory_manager and memory_manager.short_term.current_task:
            state.working_memory["current_task"] = memory_manager.short_term.current_task
        logger.info("[%s] Starting task: %s", state.task_id, user_query)
        await progress("start", "接收问题，准备分析任务", 5)

        if memory_manager and memory_manager.short_term.pending_action:
            pending_state = await handle_pending_action(
                state=state,
                user_query=user_query,
                pending=memory_manager.short_term.pending_action,
                memory_manager=memory_manager,
                agents=self._agents,
                build_agent_input=self._build_agent_input,
                progress=progress,
            )
            if pending_state is not None:
                self._save_session_context_from_state(pending_state, memory_manager)
                return pending_state

        # Step 1: Intent recognition
        await progress("intent", "识别用户意图和需要调用的 Agent", 12)
        intent_query = build_contextual_intent_query(user_query, active_task_context, task_relation)
        library_query = looks_like_library_query(user_query)
        if stored_papers and not library_query:
            titles = "; ".join(p.get("title", "?")[:60] for p in stored_papers[:5])
            intent_query = f"[Context: {len(stored_papers)} paper(s) already downloaded: {titles}]\n{user_query}"

        memory_context = (
            await memory_manager.build_agent_context(user_query)
            if memory_manager else ""
        )

        intent_input = AgentInput(
            task_id=state.task_id,
            session_id=state.session_id,
            agent_name="intent_agent",
            user_goal=user_query,
            current_stage="intent_recognition",
            input_data={
                "user_query": intent_query,
                "has_stored_papers": bool(stored_papers),
                "session_context": state.working_memory.get("session_context", {}),
            },
            context={"memory": memory_context} if memory_context else {},
        )
        intent_output = await self._agents["intent_agent"].run(intent_input, state)
        state.record_agent_output("intent_agent", intent_output.model_dump())

        if intent_output.status == AgentStatus.FAILED:
            state.add_error(f"Intent recognition failed: {intent_output.errors}")
            self._save_session_context_from_state(state, memory_manager)
            return state

        intent_result = intent_output.result
        if intent_result.get("route") == "clarify" or intent_result.get("need_clarification"):
            question = intent_result.get("clarification_question") or "我需要再确认一下：你希望我具体处理哪一部分？"
            state.workflow = "chat_workflow"
            state.record_agent_output("chat_agent", {"result": {"reply": question}})
            intent_result["workflow"] = "chat_workflow"
            intent_result["user_intent"] = "general_chat"
            state.agent_outputs["intent_agent"]["result"] = intent_result
            state.update_stage("clarification_needed", "intent_agent")
            await progress("clarify", "需要先澄清任务目标", 90)
            self._save_session_context_from_state(state, memory_manager)
            return state

        if _is_open_comparison_task(user_query, intent_result):
            intent_result["route"] = "open_task"
            intent_result["workflow"] = "general_agent_workflow"
            intent_result["intent"] = "general_open_task"
            intent_result["user_intent"] = "general_open_task"
            intent_result["suggested_agent"] = "general_agent"

        workflow = resolve_workflow(intent_result, user_query, state)
        if task_relation == "continue_task":
            continued = continuation_workflow(active_task_context, user_query)
            if continued:
                workflow = continued
        if has_image_path_marker(user_query):
            workflow = "image_understanding_workflow"
        state.workflow = workflow
        required_agents = workflow_agent_plan(workflow, user_query, state)
        user_intent = workflow_intent_label_key(workflow, intent_result.get("user_intent", ""))
        if intent_result.get("route") == "open_task" and workflow != "web_search_workflow":
            workflow = "general_agent_workflow"
            user_intent = "general_open_task"
            required_agents = _general_open_task_plan(user_query, state)
            state.workflow = workflow
        if workflow == "paper_search_workflow":
            user_intent = paper_search_intent_from_query(user_query)
        intent_result["workflow"] = workflow
        intent_result["user_intent"] = user_intent
        intent_result["required_agents"] = required_agents
        writing_source = extract_writing_source(user_query)
        if (
            workflow != "general_agent_workflow"
            and intent_result.get("route") != "open_task"
            and library_query
            and workflow not in {"kb_writing_workflow", "uploaded_file_writing_workflow"}
        ):
            user_intent = "library_qa"
            workflow = "question_answer_workflow"
            state.workflow = workflow
            required_agents = ["retrieval_agent", "reading_agent"]
            intent_result["workflow"] = workflow
            intent_result["user_intent"] = "library_qa"
            intent_result["required_agents"] = required_agents
        elif (
            should_use_cached_library_context(user_query, state)
            and not state.working_memory.get("stored_papers")
        ):
            user_intent = "library_qa"
            required_agents = ["reading_agent"]
            state.working_memory["library_qa_mode"] = True
            state.working_memory["use_cached_library_context"] = True
            intent_result["user_intent"] = "library_qa"
            intent_result["required_agents"] = required_agents

        # ── Intent routing ────────────────────────────────────────────────────
        await progress("route", f"已识别意图：{_intent_label(user_intent)}", 22)

        if workflow == "web_search_workflow" or user_intent == "web_search":
            workflow = "web_search_workflow"
            state.workflow = workflow
            user_intent = "web_search"
            required_agents = ["web_agent"]
            state.working_memory["web_read_mode"] = True
            state.working_memory["qa_source"] = "web"
            state.working_memory.pop("library_qa_mode", None)

        elif user_intent == "literature_search":
            required_agents = ["literature_agent"]
            state.working_memory["search_only"] = True

        elif user_intent == "paper_download":
            required_agents = ["literature_agent"]
            pre_selected = _resolve_download_target(
                found_papers or [],
                intent_result.get("target_indices", []),
                intent_result.get("target_keywords", ""),
            )
            state.working_memory["pre_selected_papers"] = pre_selected

        elif user_intent == "research_literature_reading":
            required_agents = ["literature_agent", "reading_agent"]
            state.working_memory["max_papers"] = 1

        elif user_intent == "paper_qa":
            if state.working_memory.get("qa_source") == "uploaded_file" or state.working_memory.get("stored_papers"):
                required_agents = ["reading_agent"]
            elif should_use_cached_library_context(user_query, state):
                required_agents = ["reading_agent"]
                state.working_memory["library_qa_mode"] = True
                state.working_memory["use_cached_library_context"] = True
            else:
                # No downloaded PDFs in this session — fall back to library search.
                # RetrievalAgent will surface a clear error if the library is also empty.
                required_agents = ["retrieval_agent", "reading_agent"]
                state.working_memory["library_qa_mode"] = True

        elif user_intent == "summarize_session":
            required_agents = ["summary_agent"]

        elif user_intent == "paper_writing":
            if workflow == "academic_writing_workflow":
                required_agents = ["writing_agent"]
                state.working_memory["writing_source"] = writing_source or "auto"
                if looks_like_writing_followup(user_query, state):
                    state.working_memory["writing_followup"] = True
            else:
                has_uploads = has_uploaded_writing_docs(state)
                if writing_source == "upload":
                    required_agents = ["writing_agent"]
                elif writing_source == "library":
                    required_agents = ["retrieval_agent", "reading_agent", "writing_agent"]
                elif writing_source == "rewrite":
                    required_agents = ["writing_agent"]
                else:
                    wants_uploads = (
                        bool(intent_result.get("use_uploaded_files"))
                        or bool(intent_result.get("use_upload"))
                        or "WRITING_MATERIAL_PATHS=" in user_query
                        or "上传" in user_query
                        or "uploaded" in user_query.lower()
                    )
                    if has_uploads and wants_uploads:
                        required_agents = ["writing_agent"]
                    elif intent_result.get("need_retrieval", intent_result.get("use_retrieval", True)):
                        required_agents = ["retrieval_agent", "reading_agent", "writing_agent"]
                    else:
                        required_agents = ["writing_agent"]
                if "retrieval_agent" in required_agents:
                    state.working_memory["library_qa_mode"] = True
                state.working_memory["writing_source"] = writing_source or "auto"

        elif user_intent in {
            "create_note", "create_note_from_chat", "create_note_from_reading",
            "update_note", "delete_note", "search_note", "embed_note",
            "reembed_note", "list_notes",
        }:
            required_agents = ["note_agent"]

        elif user_intent == "general_open_task":
            workflow = "general_agent_workflow"
            state.workflow = workflow
            required_agents = _general_open_task_plan(user_query, state)
            intent_result["workflow"] = workflow
            intent_result["required_agents"] = required_agents

        # ── New: personal library management ─────────────────────────────────

        elif user_intent == "add_to_library":
            required_agents = []

        elif user_intent == "library_qa":
            if state.working_memory.get("qa_source") == "uploaded_file":
                required_agents = ["reading_agent"]
            elif should_use_cached_library_context(user_query, state):
                required_agents = ["reading_agent"]
                state.working_memory["use_cached_library_context"] = True
            else:
                required_agents = ["retrieval_agent", "reading_agent"]
            state.working_memory["library_qa_mode"] = True

        elif user_intent == "clear_temp_rag":
            state = await self._handle_clear_temp_rag(
                state, memory_manager, session_id or state.session_id
            )
            self._save_session_context_from_state(state, memory_manager)
            return state

        logger.info("[%s] Workflow: %s | Plan: %s (intent=%s)", state.task_id, workflow, required_agents, user_intent)
        state.agent_outputs["intent_agent"]["result"] = intent_result
        await progress("plan", f"执行计划：{_agent_plan_label(required_agents)}", 30)

        # Step 2: Execute the selected workflow through LangGraph.
        state = await run_agent_workflow(
            workflow_name=workflow,
            task_state=state,
            user_query=user_query,
            agent_names=required_agents,
            agents=self._agents,
            build_agent_input=self._build_agent_input,
            progress=progress,
        )

        if workflow == "paper_search_workflow" and state.working_memory.get("search_only") and not state.pending_action:
            papers = state.agent_outputs.get("literature_agent", {}).get("result", {}).get("selected_papers", [])
            if papers:
                state.last_search_results = papers
                state.pending_action = {
                    "type": "download_choice",
                    "workflow": workflow,
                    "options": papers,
                    "message": "可以回复：下载第1篇、下载前3篇，或不下载。",
                }
        elif workflow == "conversation_summary_workflow" and not state.pending_action:
            summary_text = state.agent_outputs.get("summary_agent", {}).get("result", {}).get("final_report", "")
            if summary_text:
                state.last_summary = summary_text
                state.pending_action = {
                    "type": "save_note_choice",
                    "workflow": workflow,
                    "summary_text": summary_text,
                    "message": "是否保存到笔记？可以回复：保存，或不保存。",
                }

        # Process memory candidates
        if memory_manager:
            await progress("memory", "整理短期记忆和用户长期特征", 82)
            for agent_name in required_agents:
                agent_out = state.agent_outputs.get(agent_name, {})
                candidates = agent_out.get("memory_candidates", [])
                if candidates:
                    await memory_manager.process_candidates(candidates)
            focus = memory_manager.infer_focus_from_state(state)
            memory_manager.update_focus(focus)

        await progress("finalize", "汇总结果并生成回复", 90)
        logger.info("[%s] Task finished. Stage: %s", state.task_id, state.current_stage)
        self._save_session_context_from_state(state, memory_manager)
        return state

    # ── Library management handlers ───────────────────────────────────────────



    async def _handle_clear_temp_rag(
        self,
        state: TaskState,
        memory_manager: Optional[MemoryManager],
        session_id: str,
    ) -> TaskState:
        """Clear the session's downloaded paper list (no Chroma collection to delete)."""
        state.update_stage("clear_temp_rag", "orchestrator")

        if memory_manager:
            memory_manager.short_term.stored_papers = []
            memory_manager.short_term.found_papers = []
            memory_manager.save()

        msg = "已清除本次会话的临时论文列表。已下载的 PDF 文件仍保留在磁盘上。"
        state.record_agent_output("library_agent", {"result": {"reply": msg}})
        state.update_stage("completed", "orchestrator")
        return state

    def _build_agent_input(self, agent_name: str, user_goal: str, state: TaskState) -> AgentInput:
        input_data: dict = {}

        if agent_name == "retrieval_agent":
            input_data = {"question": user_goal}

        elif agent_name == "literature_agent":
            intent_result = state.agent_outputs.get("intent_agent", {}).get("result", {})
            search_query = intent_result.get("search_query") or user_goal
            pre_selected = state.working_memory.get("pre_selected_papers")
            input_data = {
                "query": search_query,
                "filters": {
                    "max_results": settings.default_search_max_results,
                    "sources": settings.default_search_sources,
                    "sort_by": intent_result.get("sort_by", "relevance"),
                    "title_search": intent_result.get("title_search", False),
                },
                "pre_selected_papers": pre_selected or [],
                "search_only": state.working_memory.get("search_only", False),
            }
        elif agent_name == "web_agent":
            input_data = {
                "question": user_goal,
                "urls": [],
            }
        elif agent_name == "reading_agent":
            # library_qa uses the long-term store — no documents needed
            if state.working_memory.get("library_qa_mode"):
                input_data = {
                    "documents": [],
                    "question": user_goal,
                    "mode": "library_qa",
                    "cached_library_context": state.working_memory.get("current_library_context", {})
                    if state.working_memory.get("use_cached_library_context") else {},
                }
            else:
                docs = state.tool_results.get("literature_download", {}).get("downloaded_pdfs", [])
                if not docs:
                    docs = state.working_memory.get("stored_papers", [])
                max_papers = state.working_memory.get("max_papers", len(docs))
                input_data = {
                    "documents": docs[:max_papers],
                    "question": user_goal,
                    "mode": "direct_pdf",
                }
        elif agent_name == "summary_agent":
            summary_history, summary_scope = _summary_history_for_request(
                user_goal,
                state.working_memory.get("conversation_history", []),
            )
            input_data = {
                "agent_outputs": state.agent_outputs,
                "conversation_history": summary_history,
                "summary_scope": summary_scope,
                "history_summary": state.working_memory.get("history_summary", ""),
            }
        elif agent_name == "writing_agent":
            intent_result = state.agent_outputs.get("intent_agent", {}).get("result", {})
            retrieval_result = state.agent_outputs.get("retrieval_agent", {}).get("result", {})
            writing_source = state.working_memory.get("writing_source", "auto")
            workflow_chunks = state.working_memory.get("writing_material_chunks", [])
            if writing_source in {"upload", "library", "upload_plus_library", "user_input_only"}:
                material_chunks = workflow_chunks
            else:
                material_chunks = retrieval_result.get("retrieved_chunks", [])
            input_data = {
                "user_query": user_goal,
                "writing_task_type": intent_result.get("writing_task_type", "literature_review"),
                "retrieved_chunks": material_chunks,
                "retrieval_summary": state.working_memory.get("writing_material_summary") or _build_retrieval_summary(retrieval_result),
                "constraints": intent_result.get("constraints", {}),
                "user_extra_instruction": intent_result.get("user_extra_instruction", ""),
                "user_provided_material": state.working_memory.get("user_provided_writing_material", ""),
                "source_policy": state.working_memory.get("writing_source_policy", ""),
            }
        elif agent_name == "note_agent":
            intent_result = state.agent_outputs.get("intent_agent", {}).get("result", {})
            input_data = {
                "task_type": intent_result.get("user_intent", "create_note"),
                "query": intent_result.get("target_keywords", "") or intent_result.get("search_query", ""),
                "title": intent_result.get("target_keywords", ""),
                "source_content": state.working_memory.get("pending_summary_text", ""),
                "conversation_history": state.working_memory.get("conversation_history", []),
                "agent_outputs": state.agent_outputs,
                "user_id": "local",
            }
        elif agent_name == "chat_agent":
            input_data = {
                "user_message": user_goal,
                "conversation_history": state.working_memory.get("conversation_history", []),
            }
        elif agent_name == "general_agent":
            input_data = {
                "user_message": user_goal,
                "conversation_history": state.working_memory.get("conversation_history", []),
                "history_summary": state.working_memory.get("history_summary", ""),
                "current_task": state.working_memory.get("current_task", ""),
            }

        return AgentInput(
            task_id=state.task_id,
            session_id=state.session_id,
            agent_name=agent_name,
            user_goal=user_goal,
            current_stage=state.current_stage,
            input_data=input_data,
            context={
                "working_memory": state.working_memory,
                "document_list": state.document_list,
            },
        )

    def _load_session_context(
        self,
        session_id: str,
        memory_manager: Optional[MemoryManager],
    ) -> SessionContext:
        if memory_manager:
            ctx = memory_manager.load_session_context()
        else:
            ctx = self._session_contexts.get(session_id, SessionContext(session_id=session_id))
        self._session_contexts[session_id] = ctx
        return ctx

    def _save_session_context_from_state(
        self,
        state: TaskState,
        memory_manager: Optional[MemoryManager],
    ) -> None:
        ctx = SessionContext.from_dict(state.working_memory.get("session_context", {}), state.session_id)
        ctx.recent_turns = list(state.working_memory.get("conversation_history", ctx.recent_turns))
        ctx.current_task = str(state.user_goal or state.working_memory.get("current_task") or ctx.current_task or "")
        ctx.history_summary = str(state.working_memory.get("history_summary") or ctx.history_summary or "")
        ctx.last_workflow = state.workflow or ctx.last_workflow
        output_summary = _summarize_workflow_output(state)
        if output_summary:
            ctx.last_workflow_output = output_summary
        ctx.merge_active_entities(_extract_active_entities(state))
        state.working_memory["session_context"] = ctx.to_dict()
        self._session_contexts[state.session_id] = ctx
        if memory_manager:
            memory_manager.save_session_context(ctx)


def _summarize_workflow_output(state: TaskState, max_chars: int = 4000) -> str:
    parts: list[str] = []
    workflow = state.workflow or ""
    if workflow:
        parts.append(f"Workflow: {workflow}")

    for agent_name, output in state.agent_outputs.items():
        if agent_name == "intent_agent" or not isinstance(output, dict):
            continue
        result = output.get("result", {}) or {}
        if not isinstance(result, dict):
            continue
        summary = _summarize_agent_result(agent_name, result)
        if summary:
            parts.append(f"{agent_name}: {summary}")

    return "\n\n".join(parts)[:max_chars].strip()


def _summarize_agent_result(agent_name: str, result: dict) -> str:
    if agent_name == "literature_agent":
        papers = result.get("selected_papers") or result.get("papers") or []
        titles = [str(p.get("title") or "").strip() for p in papers[:8] if p.get("title")]
        return "papers: " + "; ".join(titles) if titles else ""
    if agent_name in {"reading_agent", "web_agent"}:
        notes = result.get("reading_notes") or result.get("web_notes") or []
        if notes:
            lines = []
            for note in notes[:4]:
                title = str(note.get("title") or "result").strip()
                answer = str(note.get("answer") or "").strip()[:700]
                lines.append(f"{title}: {answer}")
            return "\n".join(lines)
        return str(result.get("reply") or result.get("answer") or "")[:1200]
    if agent_name == "retrieval_agent":
        contexts = result.get("contexts") or []
        active_title = str(result.get("active_title") or "").strip()
        prefix = f"active_title: {active_title}\n" if active_title else ""
        return prefix + "\n\n".join(str(ctx)[:500] for ctx in contexts[:3])
    if agent_name == "writing_agent":
        title = str(result.get("title") or "").strip()
        content = str(result.get("content") or result.get("reply") or "").strip()[:1500]
        return (f"{title}: " if title else "") + content
    if agent_name == "summary_agent":
        return str(result.get("final_report") or "")[:1500]
    if agent_name == "note_agent":
        return str(result.get("reply") or result.get("content") or "")[:1000]
    if agent_name == "chat_agent":
        return str(result.get("reply") or "")[:1000]
    return str(result.get("reply") or result.get("content") or result)[:1000]


def _extract_active_entities(state: TaskState) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_extract_entities_from_text(state.user_goal))

    for output in state.agent_outputs.values():
        if not isinstance(output, dict):
            continue
        result = output.get("result", {}) or {}
        if not isinstance(result, dict):
            continue
        for key in ("active_title", "title", "paper_list"):
            value = result.get(key)
            if isinstance(value, str):
                candidates.append(value)
        for paper in (result.get("selected_papers") or result.get("papers") or [])[:10]:
            if isinstance(paper, dict) and paper.get("title"):
                candidates.append(str(paper["title"]))
        metadata = result.get("metadata") or {}
        if isinstance(metadata, dict):
            for key in ("active_title", "title_filter"):
                value = metadata.get(key)
                if value:
                    candidates.append(str(value))
            candidates.extend(str(item) for item in metadata.get("libraries") or [] if item)

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        clean = str(item).strip(" \t\r\n:：,，.;；")
        if len(clean) < 2 or clean.lower() in seen:
            continue
        cleaned.append(clean[:120])
        seen.add(clean.lower())
    return cleaned[:20]


def _extract_entities_from_text(text: str) -> list[str]:
    content = text or ""
    entities: list[str] = []
    entities.extend(re.findall(r"[《“\"]([^《》“”\"]{2,80})[》”\"]", content))
    entities.extend(re.findall(r"\b[A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){1,5}\b", content))
    topic_patterns = [
        r"(?:关于|围绕|研究|分析|对比|比较|搜索|查找)\s*([\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9\s-]{1,40})",
        r"(?:about|on|for|compare|search|find)\s+([A-Za-z0-9][A-Za-z0-9\s-]{1,60})",
    ]
    for pattern in topic_patterns:
        entities.extend(match.strip() for match in re.findall(pattern, content, flags=re.IGNORECASE))
    return entities


def _resolve_download_target(
    found_papers: list[dict],
    target_indices: list[int],
    target_keywords: str,
) -> list[dict]:
    """Select papers from found_papers by 1-based index list or keyword match."""
    if not found_papers:
        return []

    if target_indices:
        result = []
        for i in target_indices:
            if 1 <= i <= len(found_papers):
                result.append(found_papers[i - 1])
        return result

    if target_keywords:
        kw = target_keywords.lower()
        return [p for p in found_papers if kw in p.get("title", "").lower()]

    return found_papers



def _build_retrieval_summary(retrieval_result: dict) -> str:
    metadata = retrieval_result.get("metadata", {})
    contexts = retrieval_result.get("contexts", [])
    lib_names = retrieval_result.get("lib_names", [])
    parts = []
    if metadata.get("chunk_count") is not None:
        parts.append(f"Retrieved {metadata.get('chunk_count')} chunks")
    if lib_names:
        parts.append("Libraries: " + ", ".join(str(x) for x in lib_names))
    if contexts:
        parts.append("Top contexts:\n" + "\n\n".join(str(c)[:800] for c in contexts[:3]))
    return "\n".join(parts)


def _summary_history_for_request(user_query: str, history: list[dict]) -> tuple[list[dict], dict[str, object]]:
    """Select the conversation slice requested by a natural summary command.

    Defaults to the whole conversation. "上一句/上两个回答" means previous assistant
    replies, because the user normally wants to summarize what the agent just said.
    """
    q = (user_query or "").lower().strip()
    messages = [m for m in history if str(m.get("content") or "").strip()]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    reply_count = _requested_previous_reply_count(q)
    if reply_count:
        selected = assistant_messages[-reply_count:]
        return selected, {
            "mode": "previous_assistant_replies",
            "requested_count": reply_count,
            "selected_count": len(selected),
            "description": f"Summarize the previous {reply_count} assistant repl{'y' if reply_count == 1 else 'ies'}.",
        }

    turn_count = _requested_previous_turn_count(q)
    if turn_count:
        selected = _last_turns(messages, turn_count)
        return selected, {
            "mode": "previous_turns",
            "requested_count": turn_count,
            "selected_count": len(selected),
            "description": f"Summarize the previous {turn_count} conversation turn{'s' if turn_count != 1 else ''}.",
        }

    return messages, {
        "mode": "whole_conversation",
        "requested_count": None,
        "selected_count": len(messages),
        "description": "Summarize the whole conversation so far.",
    }


def _requested_previous_reply_count(q: str) -> int:
    if any(marker in q for marker in ["上一句", "上句", "上个回答", "上一个回答", "上一条回复", "上一次回复", "last reply", "previous reply"]):
        return 1

    patterns = [
        r"(?:上|最近|前)\s*(\d+)\s*(?:句|条回复|个回复|个回答|次回复|answers?|replies)",
        r"(?:last|previous)\s*(\d+)\s*(?:answers?|replies|messages)",
    ]
    for pattern in patterns:
        match = re.search(pattern, q)
        if match:
            return max(1, min(int(match.group(1)), 20))

    chinese_numerals = {
        "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    match = re.search(r"(?:上|最近|前)\s*([一两二三四五六七八九十])\s*(?:句|条回复|个回复|个回答|次回复)", q)
    if match:
        return chinese_numerals.get(match.group(1), 0)
    return 0


def _requested_previous_turn_count(q: str) -> int:
    if any(marker in q for marker in ["上一轮", "上轮", "上一次对话", "last turn", "previous turn"]):
        return 1
    match = re.search(r"(?:上|最近|前)\s*(\d+)\s*(?:轮|轮对话|turns?)", q)
    if match:
        return max(1, min(int(match.group(1)), 20))
    chinese_numerals = {
        "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    match = re.search(r"(?:上|最近|前)\s*([一两二三四五六七八九十])\s*(?:轮|轮对话)", q)
    if match:
        return chinese_numerals.get(match.group(1), 0)
    return 0


def _last_turns(messages: list[dict], count: int) -> list[dict]:
    turns: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        current.append(message)
        if message.get("role") == "assistant":
            turns.append(current)
            current = []
    if current:
        turns.append(current)
    selected: list[dict] = []
    for turn in turns[-count:]:
        selected.extend(turn)
    return selected


def _general_open_task_plan(user_query: str, state: TaskState) -> list[str]:
    """Plan lightweight subtasks for the General Agent.

    The General Agent remains the final synthesizer, while obvious retrieval/search
    needs can run first as sub-capabilities.
    """
    q = user_query.lower()
    agents: list[str] = []

    library_markers = [
        "知识库", "文献库", "资料库", "knowledge base", "my library", "literature library",
    ]
    paper_support_markers = [
        "论文", "文献", "找几篇", "找一些", "支撑", "相关研究",
        "papers", "literature", "related work", "supporting papers", "search papers",
    ]

    library_markers.extend([
        "\u77e5\u8bc6\u5e93", "\u6587\u732e\u5e93", "\u8d44\u6599\u5e93",
        "knowledge base", "my library", "literature library",
    ])
    paper_support_markers.extend([
        "\u8bba\u6587", "\u6587\u732e", "\u627e\u51e0\u7bc7", "\u627e\u4e00\u4e9b",
        "\u652f\u6491", "\u76f8\u5173\u7814\u7a76",
        "papers", "literature", "related work", "supporting papers", "search papers",
    ])
    stored_paper_markers = [
        "\u8fd9\u7bc7", "\u8fd9\u4e2a\u6587\u4ef6", "\u8fd9\u4efd",
        "this paper", "this file", "pdf",
    ]

    if any(marker in q for marker in library_markers):
        state.working_memory["library_qa_mode"] = True
        agents.extend(["retrieval_agent", "reading_agent"])
    elif any(marker in q for marker in paper_support_markers):
        state.working_memory["search_only"] = True
        agents.append("literature_agent")
    elif state.working_memory.get("stored_papers") and any(marker in q for marker in stored_paper_markers):
        agents.append("reading_agent")
    elif state.working_memory.get("stored_papers") and any(
        marker in q for marker in ["这篇", "这个文件", "这份", "this paper", "this file", "pdf"]
    ):
        agents.append("reading_agent")

    agents.append("general_agent")
    return list(dict.fromkeys(agents))


def _is_open_comparison_task(user_query: str, intent_result: dict) -> bool:
    """Comparison across papers needs synthesis, so keep it on the General Agent route."""
    intent = str(intent_result.get("intent") or "").lower()
    workflow = str(intent_result.get("workflow") or "")
    if intent in {"compare_papers", "paper_comparison", "compare", "contrast_papers", "synthesize_papers"}:
        return True
    if workflow == "general_agent_workflow":
        return False
    q = user_query.lower()
    markers = [
        "对比", "比较", "不同", "差异", "区别", "异同", "优缺点", "优劣",
        "compare", "comparison", "contrast", "difference", "differences",
        "different", "pros and cons", "versus", " vs ",
    ]
    return any(marker in q for marker in markers)




def _intent_label(intent: str) -> str:
    labels = {
        "literature_search": "文献搜索",
        "web_search": "网页搜索",
        "paper_download": "论文下载",
        "research_literature_reading": "检索并阅读论文",
        "paper_qa": "当前论文问答",
        "library_qa": "知识库问答",
        "image_understanding": "图片理解问答",
        "add_to_library": "加入知识库",
        "clear_temp_rag": "清理临时文档",
        "summarize_session": "总结对话",
        "general_chat": "通用对话",
        "create_note": "创建笔记",
        "create_note_from_chat": "对话转笔记",
        "create_note_from_reading": "阅读结果转笔记",
        "update_note": "更新笔记",
        "delete_note": "删除笔记",
        "search_note": "搜索笔记",
        "embed_note": "笔记向量化",
        "reembed_note": "重新向量化笔记",
        "list_notes": "查看笔记",
    }
    return labels.get(intent, intent or "待确认")


def _agent_plan_label(agents: list[str]) -> str:
    return " → ".join(_agent_short_name(a) for a in agents if a != "intent_agent") or "直接处理"


def _agent_short_name(agent_name: str) -> str:
    labels = {
        "literature_agent": "文献搜索",
        "retrieval_agent": "知识库检索",
        "reading_agent": "阅读",
        "web_agent": "网页搜索阅读",
        "note_agent": "笔记",
        "summary_agent": "总结",
        "chat_agent": "回答",
        "analyze_image": "图片分析",
    }
    return labels.get(agent_name, agent_name)


def _agent_running_text(agent_name: str) -> str:
    labels = {
        "literature_agent": "正在检索、筛选或下载相关论文",
        "retrieval_agent": "正在从知识库检索相关文档片段并评估检索质量",
        "reading_agent": "正在读取文档片段并生成基于上下文的回答",
        "web_agent": "正在搜索网页、抓取正文并综合回答",
        "note_agent": "正在处理笔记内容",
        "summary_agent": "正在压缩上下文并生成总结",
        "chat_agent": "正在组织回答",
    }
    return labels.get(agent_name, f"正在运行 {agent_name}")


def _agent_done_text(agent_name: str) -> str:
    labels = {
        "literature_agent": "文献检索阶段完成",
        "retrieval_agent": "知识库检索完成",
        "reading_agent": "阅读分析阶段完成",
        "web_agent": "网页搜索阅读完成",
        "note_agent": "笔记处理完成",
        "summary_agent": "总结生成完成",
        "chat_agent": "回答生成完成",
    }
    return labels.get(agent_name, f"{agent_name} 完成")
