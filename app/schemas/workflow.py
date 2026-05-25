from typing import Optional

from pydantic import BaseModel, Field


WORKFLOW_NAMES = {
    "paper_search_workflow",
    "conversation_summary_workflow",
    "question_answer_workflow",
    "image_understanding_workflow",
    "academic_writing_workflow",
    "general_agent_workflow",
    "research_agent_workflow",
    "web_search_workflow",
}

# Deterministic "actions" — plain verbs with no branching reasoning, dispatched
# directly by the orchestrator (app/orchestrator/actions.py) rather than compiled
# into a LangGraph. They share the intent layer's `workflow` routing field but
# are a distinct category: anything ending in `_action` is NOT a graph.
ACTION_NAMES = {
    "library_ingest_action",
    "note_action",
}

# Valid route targets the intent layer may emit: a graph workflow or an action.
ROUTE_TARGET_NAMES = WORKFLOW_NAMES | ACTION_NAMES

# Routes that used to exist under a different name but were renamed/merged. Any
# legacy session JSON or older LLM output that still names them gets normalised
# to the live replacement at load time. The `*_workflow → *_action` entries are
# from demoting those two graphs to direct action handlers.
_LEGACY_WORKFLOW_ALIASES = {
    "kb_writing_workflow": "academic_writing_workflow",
    "uploaded_file_writing_workflow": "academic_writing_workflow",
    "chat_workflow": "general_agent_workflow",
    "library_ingest_workflow": "library_ingest_action",
    "note_workflow": "note_action",
}


class WorkflowIntent(BaseModel):
    intent: str = "chat"
    workflow: str = "general_agent_workflow"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    need_clarification: bool = False
    clarification_question: Optional[str] = None
    reason: str = ""


def normalize_workflow_intent(raw: dict) -> dict:
    """Normalize new workflow intent JSON while tolerating legacy intent payloads."""
    intent = str(raw.get("intent") or raw.get("user_intent") or "chat").strip() or "chat"
    workflow = str(raw.get("workflow") or _legacy_workflow(intent) or "general_agent_workflow").strip()
    # Redirect any legacy/merged-away workflow names to their live replacements.
    workflow = _LEGACY_WORKFLOW_ALIASES.get(workflow, workflow)
    if workflow not in ROUTE_TARGET_NAMES:
        workflow = "general_agent_workflow"
    confidence = raw.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.5
    result = WorkflowIntent(
        intent=intent,
        workflow=workflow,
        confidence=confidence,
        need_clarification=bool(raw.get("need_clarification", False)),
        clarification_question=raw.get("clarification_question"),
        reason=str(raw.get("reason") or ""),
    ).model_dump()
    # Preserve useful legacy fields if an older prompt/model returns them.
    for key in (
        "route", "workflow_name", "missing_slots", "user_goal", "suggested_agent",
        "search_query", "title_search", "download_target", "target_keywords",
        "target_indices", "sort_by", "writing_task_type", "constraints",
        "need_retrieval", "use_retrieval", "user_extra_instruction",
        "local_paths", "file_paths", "source_paths", "lib_id",
    ):
        if key in raw:
            result[key] = raw[key]
    result["user_intent"] = _workflow_legacy_intent(workflow, intent)
    return result


def _legacy_workflow(intent: str) -> str:
    mapping = {
        "literature_search": "paper_search_workflow",
        "paper_download": "paper_search_workflow",
        "research_literature_reading": "paper_search_workflow",
        "summarize_session": "conversation_summary_workflow",
        "paper_qa": "question_answer_workflow",
        "library_qa": "question_answer_workflow",
        "image_understanding": "image_understanding_workflow",
        "add_to_library": "library_ingest_action",
        "paper_writing": "academic_writing_workflow",
        "create_note": "note_action",
        "create_note_from_chat": "note_action",
        "create_note_from_summary": "note_action",
        "create_note_from_reading": "note_action",
        "update_note": "note_action",
        "delete_note": "note_action",
        "search_note": "note_action",
        "embed_note": "note_action",
        "reembed_note": "note_action",
        "list_notes": "note_action",
        # chat is no longer a separate workflow — General Agent handles it.
        "general_chat": "general_agent_workflow",
        "chat": "general_agent_workflow",
    }
    return mapping.get(intent, "")


def _workflow_legacy_intent(workflow: str, fallback_intent: str) -> str:
    mapping = {
        "paper_search_workflow": "literature_search",
        "conversation_summary_workflow": "summarize_session",
        "question_answer_workflow": "library_qa",
        "image_understanding_workflow": "image_understanding",
        "library_ingest_action": "add_to_library",
        "academic_writing_workflow": "paper_writing",
        "note_action": fallback_intent if fallback_intent.endswith("note") or "note" in fallback_intent else "create_note",
        "general_agent_workflow": "general_open_task",
        "research_agent_workflow": "research_task",
        "web_search_workflow": "web_search",
    }
    return mapping.get(workflow, fallback_intent or "general_open_task")
