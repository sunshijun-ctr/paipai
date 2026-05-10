import time
import uuid
from typing import Any, Optional
from pydantic import BaseModel, Field


class TaskState(BaseModel):
    task_id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    session_id: str = Field(default_factory=lambda: f"session_{uuid.uuid4().hex[:8]}")
    user_goal: str = ""
    current_stage: str = "pending"
    active_agent: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    agent_outputs: dict[str, Any] = Field(default_factory=dict)
    tool_results: dict[str, Any] = Field(default_factory=dict)

    document_list: list[str] = Field(default_factory=list)
    temporary_rag_refs: list[str] = Field(default_factory=list)
    long_term_rag_refs: list[str] = Field(default_factory=list)

    working_memory: dict[str, Any] = Field(default_factory=dict)
    workflow: Optional[str] = None
    pending_action: Optional[dict[str, Any]] = None
    active_files: list[dict[str, Any]] = Field(default_factory=list)
    active_file_id: Optional[str] = None
    last_search_results: list[dict[str, Any]] = Field(default_factory=list)
    last_summary: Optional[str] = None
    last_answer: Optional[str] = None

    summary: Optional[str] = None
    next_action: Optional[str] = None
    errors: list[str] = Field(default_factory=list)

    def update_stage(self, stage: str, agent: Optional[str] = None) -> None:
        self.current_stage = stage
        self.active_agent = agent
        self.updated_at = time.time()

    def record_agent_output(self, agent_name: str, output: Any) -> None:
        self.agent_outputs[agent_name] = output
        self.updated_at = time.time()

    def add_error(self, error: str) -> None:
        self.errors.append(error)
        self.updated_at = time.time()
