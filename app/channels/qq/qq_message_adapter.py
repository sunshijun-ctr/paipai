from typing import Any

from app.channels.qq.qq_event_utils import (
    author_id,
    clean_mention_text,
    detect_scene,
    event_data,
    event_id,
    event_type,
    message_id,
)
from app.channels.qq.qq_session_mapper import QQSessionMapper
from app.schemas.internal_message import Attachment, InternalChatMessage


class QQMessageAdapter:
    def __init__(self, session_mapper: QQSessionMapper | None = None) -> None:
        self.session_mapper = session_mapper or QQSessionMapper()

    def to_internal(self, raw_event: dict[str, Any]) -> InternalChatMessage:
        data = event_data(raw_event)
        scene = detect_scene(raw_event)
        raw_text = str(data.get("content") or data.get("text") or "").strip()
        text = clean_mention_text(raw_text, raw_event) if scene != "private" else raw_text
        msg_id = message_id(raw_event) or event_id(raw_event)
        if not msg_id:
            raise ValueError("QQ event missing message id")

        user_id = author_id(raw_event)
        if not user_id:
            raise ValueError("QQ event missing author id")

        return InternalChatMessage(
            channel="qq",
            platform_user_id=user_id,
            session_id=self.session_mapper.get_session_id(raw_event),
            conversation_type="guild" if scene == "channel" else scene,
            message_id=msg_id,
            raw_event_id=event_id(raw_event),
            text=text,
            attachments=self._attachments(data),
            metadata={
                "qq_scene": scene,
                "qq_event_type": event_type(raw_event),
                "group_openid": data.get("group_openid") or data.get("group_id"),
                "guild_id": data.get("guild_id"),
                "channel_id": data.get("channel_id"),
                "raw_event": raw_event,
            },
        )

    def _attachments(self, data: dict[str, Any]) -> list[Attachment]:
        items = data.get("attachments") or []
        result: list[Attachment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("content_type") or item.get("type") or "").lower()
            attachment_type = "unknown"
            if content_type.startswith("image") or content_type in {"png", "jpg", "jpeg"}:
                attachment_type = "image"
            elif content_type.startswith("audio"):
                attachment_type = "audio"
            elif content_type.startswith("video"):
                attachment_type = "video"
            elif item.get("filename") or item.get("file_id"):
                attachment_type = "file"
            result.append(
                Attachment(
                    type=attachment_type,
                    url=item.get("url"),
                    filename=item.get("filename"),
                    file_id=item.get("file_id") or item.get("id"),
                    mime_type=item.get("content_type"),
                    metadata=item,
                )
            )
        return result
