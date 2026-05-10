from typing import Any

from app.channels.qq.qq_event_utils import author_id, detect_scene, event_data


class QQSessionMapper:
    def get_session_id(self, event: dict[str, Any]) -> str:
        data = event_data(event)
        scene = detect_scene(event)

        if scene == "private":
            openid = author_id(event)
            if not openid:
                raise ValueError("QQ private message missing author openid")
            return f"qq_private_{openid}"

        if scene == "group":
            group_openid = str(data.get("group_openid") or data.get("group_id") or "")
            member_openid = author_id(event)
            if not group_openid or not member_openid:
                raise ValueError("QQ group message missing group or member openid")
            return f"qq_group_{group_openid}_{member_openid}"

        guild_id = str(data.get("guild_id") or "")
        channel_id = str(data.get("channel_id") or "")
        user_id = author_id(event)
        if not guild_id or not channel_id or not user_id:
            raise ValueError("QQ channel message missing guild, channel, or user id")
        return f"qq_guild_{guild_id}_{channel_id}_{user_id}"
