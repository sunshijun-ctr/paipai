from typing import Optional

from pydantic import BaseModel, Field


WORKFLOW_NAMES = {
    "paper_search_workflow",
    "conversation_summary_workflow",
    "question_answer_workflow",
    "image_understanding_workflow",
    "library_ingest_workflow",
    "academic_writing_workflow",
    "kb_writing_workflow",
    "uploaded_file_writing_workflow",
    "note_workflow",
    "general_agent_workflow",
    "chat_workflow",
}


class WorkflowIntent(BaseModel):
    intent: str = "chat"
    workflow: str = "chat_workflow"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    need_clarification: bool = False
    clarification_question: Optional[str] = None
    reason: str = ""


def normalize_workflow_intent(raw: dict) -> dict:
    """Normalize new workflow intent JSON while tolerating legacy intent payloads."""
    intent = str(raw.get("intent") or raw.get("user_intent") or "chat").strip() or "chat"
    workflow = str(raw.get("workflow") or _legacy_workflow(intent) or "chat_workflow").strip()
    if workflow not in WORKFLOW_NAMES:
        workflow = "chat_workflow"
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
        "add_to_library": "library_ingest_workflow",
        "paper_writing": "academic_writing_workflow",
        "create_note": "note_workflow",
        "create_note_from_chat": "note_workflow",
        "create_note_from_summary": "note_workflow",
        "create_note_from_reading": "note_workflow",
        "update_note": "note_workflow",
        "delete_note": "note_workflow",
        "search_note": "note_workflow",
        "embed_note": "note_workflow",
        "reembed_note": "note_workflow",
        "list_notes": "note_workflow",
        "general_chat": "chat_workflow",
    }
    return mapping.get(intent, "")


def _workflow_legacy_intent(workflow: str, fallback_intent: str) -> str:
    mapping = {
        "paper_search_workflow": "literature_search",
        "conversation_summary_workflow": "summarize_session",
        "question_answer_workflow": "library_qa",
        "image_understanding_workflow": "image_understanding",
        "library_ingest_workflow": "add_to_library",
        "academic_writing_workflow": "paper_writing",
        "kb_writing_workflow": "paper_writing",
        "uploaded_file_writing_workflow": "paper_writing",
        "note_workflow": fallback_intent if fallback_intent.endswith("note") or "note" in fallback_intent else "create_note",
        "general_agent_workflow": "general_open_task",
        "chat_workflow": "general_chat",
    }
    return mapping.get(workflow, fallback_intent or "general_chat")
