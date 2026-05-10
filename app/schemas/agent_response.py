from typing import Any

from pydantic import BaseModel, Field


class AgentResponse(BaseModel):
    text: str
    success: bool = True
    response_type: str = "text"
    citations: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
