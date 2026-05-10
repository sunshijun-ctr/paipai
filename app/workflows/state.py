from typing import Any, Optional

from pydantic import BaseModel, Field


class ResearchAssistantState(BaseModel):
    session_id: str
    workflow: str
    user_message: str
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    active_files: list[dict[str, Any]] = Field(default_factory=list)
    active_file_id: Optional[str] = None
    last_search_results: list[dict[str, Any]] = Field(default_factory=list)
    last_summary: Optional[str] = None
    last_answer: Optional[str] = None
    pending_action: Optional[dict[str, Any]] = None
    working_memory: dict[str, Any] = Field(default_factory=dict)
