from typing import Any, TypedDict


class WorkingMemory(TypedDict, total=False):
    # Task identity
    task_id: str
    user_goal: str
    current_stage: str
    active_agent: str

    # Paper tracking
    candidate_papers: list[dict]
    selected_papers: list[dict]
    current_document: str
    current_question: str

    # Tool outputs
    tool_results_refs: dict[str, Any]
    temporary_rag_refs: list[str]

    # Answer drafting
    draft_answer: str
    next_action: str
    errors: list[str]

    # Session context (injected by orchestrator from MemoryManager)
    conversation_history: list[dict]
    stored_papers: list[dict]
    found_papers: list[dict]
    indexed_titles: list[str]

    # Control flags
    search_only: bool
    pre_selected_papers: list[dict]
    max_papers: int
