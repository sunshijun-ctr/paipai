from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Attachment(BaseModel):
    type: Literal["image", "file", "audio", "video", "unknown"]
    url: Optional[str] = None
    filename: Optional[str] = None
    file_id: Optional[str] = None
    mime_type: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InternalChatMessage(BaseModel):
    channel: Literal["qq", "web", "wechat", "feishu"]
    platform_user_id: str
    session_id: str
    conversation_type: Literal["private", "group", "guild", "channel"]
    message_id: str
    raw_event_id: Optional[str] = None
    text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
