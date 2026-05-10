from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class Note(BaseModel):
    id: str
    user_id: str = "local"
    title: str
    content_markdown: str
    source_type: str = "manual"
    source_id: str = ""
    paper_id: str = ""
    conversation_id: str = ""
    tags: list[str] = Field(default_factory=list)
    embedding_status: str = "not_embedded"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)


class NoteCreate(BaseModel):
    user_id: str = "local"
    title: str
    content_markdown: str = ""
    source_type: str = "manual"
    source_id: str = ""
    paper_id: str = ""
    conversation_id: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content_markdown: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    paper_id: Optional[str] = None
    conversation_id: Optional[str] = None
    tags: Optional[list[str]] = None
    embedding_status: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
