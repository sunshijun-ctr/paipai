import re
from typing import Any, Literal

QQScene = Literal["private", "group", "channel"]


def event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("d", event)
    return data if isinstance(data, dict) else {}


def event_type(event: dict[str, Any]) -> str:
    return str(event.get("t") or event.get("type") or "").upper()


def detect_scene(event: dict[str, Any]) -> QQScene:
    data = event_data(event)
    typ = event_type(event)
    if "GROUP" in typ or data.get("group_openid") or data.get("group_id"):
        return "group"
    if data.get("guild_id") or data.get("channel_id") or "GUILD" in typ or typ in {
        "AT_MESSAGE_CREATE",
        "MESSAGE_CREATE",
    }:
        return "channel"
    return "private"


def message_id(event: dict[str, Any]) -> str:
    data = event_data(event)
    return str(
        data.get("id")
        or data.get("msg_id")
        or data.get("message_id")
        or event.get("id")
        or event.get("s")
        or ""
    )


def event_id(event: dict[str, Any]) -> str:
    return str(event.get("id") or event.get("event_id") or event.get("s") or message_id(event))


def author_id(event: dict[str, Any]) -> str:
    data = event_data(event)
    author = data.get("author") or data.get("member") or {}
    if not isinstance(author, dict):
        author = {}
    return str(
        author.get("member_openid")
        or author.get("user_openid")
        or author.get("openid")
        or author.get("id")
        or data.get("openid")
        or data.get("user_openid")
        or data.get("author_id")
        or ""
    )


def clean_mention_text(text: str, event: dict[str, Any]) -> str:
    cleaned = text or ""
    data = event_data(event)
    for mention in data.get("mentions") or []:
        if not isinstance(mention, dict):
            continue
        for key in ("id", "user_openid", "username", "nick"):
            value = str(mention.get(key) or "").strip()
            if value:
                cleaned = cleaned.replace(f"<@!{value}>", " ")
                cleaned = cleaned.replace(f"<@{value}>", " ")
                cleaned = cleaned.replace(f"@{value}", " ")
    cleaned = re.sub(r"<@!?\w+>", " ", cleaned)
    cleaned = re.sub(r"@\S+\s+", " ", cleaned, count=1)
    return re.sub(r"\s+", " ", cleaned).strip()
