from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_SUCCESS = "partial_success"


class AgentInput(BaseModel):
    task_id: str
    session_id: str
    agent_name: str
    user_goal: str
    current_stage: str
    input_data: dict[str, Any] = Field(default_factory=dict)  #Field(default_factory=dict)防止字典污染，每次调用会生成独立的字典实例
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    task_id: str
    session_id: str
    agent_name: str
    status: AgentStatus
    result: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    next_suggestion: str = ""
    errors: list[str] = Field(default_factory=list)
    # Memory candidates produced by this agent — MemoryManager is the sole writer to long-term memory
    memory_candidates: list[dict[str, Any]] = Field(default_factory=list)
