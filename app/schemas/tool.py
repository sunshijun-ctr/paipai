from typing import Any, Optional
from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
