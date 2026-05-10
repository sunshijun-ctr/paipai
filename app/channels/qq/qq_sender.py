from typing import Any

from app.channels.qq.qq_client import QQClient
from app.channels.qq.qq_event_utils import author_id, detect_scene, event_data, event_id, message_id


def format_for_qq(text: str, max_len: int = 1800) -> str:
    cleaned = (text or "").strip().replace("<br>", "\n")
    if not cleaned:
        cleaned = "我暂时没有生成有效回复，请换一种问法再试一次。"
    if len(cleaned) <= max_len:
        return cleaned
    suffix = "\n\n[内容较长，已截断。请到 Web 端查看完整结果。]"
    return cleaned[: max_len - len(suffix)] + suffix


class QQSender:
    def __init__(self, qq_client: QQClient, max_reply_length: int = 1800) -> None:
        self.qq_client = qq_client
        self.max_reply_length = max_reply_length

    async def reply(self, raw_event: dict[str, Any], text: str) -> dict[str, Any]:
        scene = detect_scene(raw_event)
        safe_text = format_for_qq(text, self.max_reply_length)
        msg_id = message_id(raw_event) or None
        raw_event_id = event_id(raw_event) or None
        data = event_data(raw_event)

        if scene == "private":
            openid = author_id(raw_event)
            return await self.qq_client.send_private_message(
                openid,
                safe_text,
                msg_id,
                raw_event_id,
            )

        if scene == "group":
            group_openid = str(data.get("group_openid") or data.get("group_id") or "")
            return await self.qq_client.send_group_message(
                group_openid,
                safe_text,
                msg_id,
                raw_event_id,
            )

        channel_id = str(data.get("channel_id") or "")
        return await self.qq_client.send_channel_message(
            channel_id,
            safe_text,
            msg_id,
            raw_event_id,
        )
