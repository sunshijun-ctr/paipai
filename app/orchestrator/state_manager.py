from app.memory.manager import MemoryManager
from app.orchestrator.task_context import build_task_context_from_state, looks_like_missing_material_reply
from app.session.context import SessionContext
from app.state.task_state import TaskState


def sync_memory_from_task_state(
    *,
    state: TaskState,
    user_text: str,
    assistant_reply: str,
    memory_manager: MemoryManager,
) -> None:
    """Persist workflow outputs that should survive into the next user turn."""
    intent_out = state.agent_outputs.get("intent_agent", {})
    user_intent = intent_out.get("result", {}).get("user_intent", "")

    lit = state.agent_outputs.get("literature_agent", {})
    if lit:
        result = lit.get("result", {})
        if user_intent in {"literature_search", "research_literature_reading"}:
            new_found = result.get("selected_papers", [])
            if new_found:
                memory_manager.short_term.found_papers = new_found

        new_downloads = lit.get("artifacts", {}).get("downloaded_pdfs", [])
        for paper in new_downloads:
            if paper not in memory_manager.short_term.stored_papers:
                memory_manager.short_term.stored_papers.append(paper)

    retrieval = state.agent_outputs.get("retrieval_agent", {})
    if retrieval:
        cache = build_library_context_cache(retrieval.get("result", {}))
        if cache:
            memory_manager.short_term.current_library_context = cache
    else:
        reading = state.agent_outputs.get("reading_agent", {})
        if reading:
            cache = build_library_context_cache_from_reading(reading.get("result", {}))
            if cache:
                memory_manager.short_term.current_library_context = cache

    if user_intent == "clear_temp_rag":
        memory_manager.short_term.stored_papers = []
        memory_manager.short_term.found_papers = []

    writing = state.agent_outputs.get("writing_agent", {})
    if writing:
        cache = build_writing_context_cache(writing.get("result", {}), assistant_reply)
        if cache:
            previous_writing_ctx = memory_manager.short_term.current_writing_context or {}
            if (
                state.working_memory.get("task_relation") == "continue_task"
                and previous_writing_ctx.get("content")
                and looks_like_missing_material_reply(cache.get("content", ""))
            ):
                cache = previous_writing_ctx
            memory_manager.short_term.current_writing_context = cache

    previous_active_ctx = memory_manager.short_term.active_task_context or {}
    active_ctx = build_task_context_from_state(state, assistant_reply)
    if (
        state.working_memory.get("task_relation") == "continue_task"
        and previous_active_ctx.get("subject_content")
        and looks_like_missing_material_reply(active_ctx.get("subject_content", ""))
    ):
        active_ctx = {
            **previous_active_ctx,
            "last_workflow": active_ctx.get("last_workflow", previous_active_ctx.get("last_workflow", "")),
            "last_stage": active_ctx.get("last_stage", previous_active_ctx.get("last_stage", "")),
            "last_agent_outputs_summary": active_ctx.get(
                "last_agent_outputs_summary",
                previous_active_ctx.get("last_agent_outputs_summary", {}),
            ),
        }
    if active_ctx.get("subject_content"):
        memory_manager.short_term.active_task_context = active_ctx

    if assistant_reply:
        memory_manager.update_after_turn(user_text, assistant_reply)

    ctx = SessionContext.from_dict(
        state.working_memory.get("session_context") or memory_manager.short_term.session_context.to_dict(),
        session_id=state.session_id,
    )
    ctx.recent_turns = memory_manager.short_term.get_full_history()
    ctx.current_task = user_text.strip() or ctx.current_task
    ctx.history_summary = memory_manager.short_term.history_summary
    ctx.last_workflow = state.workflow or ctx.last_workflow
    if assistant_reply:
        ctx.last_workflow_output = assistant_reply[:4000]
    memory_manager.save_session_context(ctx)
    state.working_memory["session_context"] = ctx.to_dict()

    memory_manager.short_term.pending_action = state.pending_action
    memory_manager.save()


def build_library_context_cache(result: dict) -> dict:
    contexts = result.get("contexts", [])
    active_title = result.get("active_title", "")
    chunks = result.get("retrieved_chunks", [])

    if active_title and chunks:
        active_contexts = [
            f"[{c.get('metadata', {}).get('title', 'Unknown')} / {c.get('metadata', {}).get('section', '?').upper()}]\n{c.get('document', '')}"
            for c in chunks
            if c.get("document") and c.get("metadata", {}).get("title", "") == active_title
        ]
        if active_contexts:
            contexts = active_contexts

    if not contexts:
        return {}

    return {
        "contexts": contexts,
        "paper_list": result.get("paper_list", ""),
        "lib_names": result.get("lib_names", []),
        "active_title": active_title,
        "question": result.get("question", ""),
        "original_question": result.get("original_question", ""),
        "title_filter": result.get("title_filter", ""),
        "metadata": result.get("metadata", {}),
    }


def build_writing_context_cache(result: dict, assistant_reply: str = "") -> dict:
    content = str(result.get("content") or "").strip()
    if not content and assistant_reply:
        content = str(assistant_reply).strip()
    if not content:
        return {}
    citations = result.get("citations") or []
    return {
        "title": result.get("title") or "",
        "task_type": result.get("task_type") or "",
        "content": content,
        "citations": citations,
        "material_usage_summary": result.get("material_usage_summary") or "",
    }


def build_library_context_cache_from_reading(result: dict) -> dict:
    metadata = result.get("metadata", {})
    if metadata.get("retriever") != "long_term_library":
        return {}

    contexts = result.get("contexts", [])
    if not contexts:
        return {}

    active_title = metadata.get("active_title", "")
    return {
        "contexts": contexts,
        "paper_list": active_title,
        "lib_names": metadata.get("libraries", []),
        "active_title": active_title,
        "question": result.get("question", ""),
        "original_question": result.get("question", ""),
        "title_filter": metadata.get("title_filter", ""),
        "metadata": metadata,
    }
