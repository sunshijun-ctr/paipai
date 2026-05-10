import logging
import time
from typing import Any, Optional

from app.channels.qq.qq_config import QQConfig
from app.channels.qq.qq_deduplicator import InMemoryDeduplicator
from app.channels.qq.qq_event_utils import event_id, message_id
from app.channels.qq.qq_message_adapter import QQMessageAdapter
from app.channels.qq.qq_sender import QQSender, format_for_qq
from app.channels.qq.qq_signature import build_webhook_validation_response
from app.services.agent_service import AgentService

logger = logging.getLogger(__name__)


class QQEventReceiver:
    def __init__(
        self,
        agent_service: AgentService,
        sender: Optional[QQSender] = None,
        adapter: Optional[QQMessageAdapter] = None,
        deduplicator: Optional[InMemoryDeduplicator] = None,
        config: Optional[QQConfig] = None,
    ) -> None:
        self.agent_service = agent_service
        self.sender = sender
        self.adapter = adapter or QQMessageAdapter()
        self.deduplicator = deduplicator or InMemoryDeduplicator()
        self.config = config or QQConfig()

    async def handle_webhook(self, raw_event: dict[str, Any]) -> dict[str, Any]:
        if raw_event.get("op") == 13:
            return build_webhook_validation_response(raw_event, self.config)
        if not self._looks_like_message_event(raw_event):
            logger.info(
                "Ignore unsupported QQ webhook event op=%s type=%s",
                raw_event.get("op"),
                raw_event.get("t") or raw_event.get("type"),
            )
            return {"success": True, "ignored": True}
        return await self.handle_qq_event(raw_event)

    async def handle_qq_event(self, raw_event: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        dedup_key = message_id(raw_event) or event_id(raw_event)
        if self.config.enable_message_dedup and dedup_key:
            if self.deduplicator.exists(dedup_key):
                logger.info("Drop duplicate QQ event: %s", dedup_key)
                return {"success": True, "duplicate": True}
            self.deduplicator.mark(dedup_key)

        internal_msg = self.adapter.to_internal(raw_event)
        if not self._is_allowed(internal_msg):
            logger.warning("QQ message rejected by whitelist: %s", internal_msg.session_id)
            return {"success": False, "error": "not_allowed"}

        response = await self.agent_service.chat(internal_msg)
        reply_text = format_for_qq(response.text, self.config.max_reply_length)
        send_result: dict[str, Any] = {}
        if self.sender:
            send_result = await self.sender.reply(raw_event, reply_text)

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "QQ message handled session=%s message=%s success=%s latency_ms=%s",
            internal_msg.session_id,
            internal_msg.message_id,
            response.success,
            latency_ms,
        )
        return {
            "success": response.success,
            "duplicate": False,
            "session_id": internal_msg.session_id,
            "message_id": internal_msg.message_id,
            "reply": reply_text,
            "send_result": send_result,
            "latency_ms": latency_ms,
            "metadata": response.metadata,
        }

    def _is_allowed(self, message) -> bool:
        if not self.config.enable_user_whitelist:
            return True
        if message.platform_user_id in self.config.allowed_user_set:
            return True
        group_openid = str(message.metadata.get("group_openid") or "")
        return bool(group_openid and group_openid in self.config.allowed_group_set)

    def _looks_like_message_event(self, raw_event: dict[str, Any]) -> bool:
        data = raw_event.get("d", raw_event)
        if not isinstance(data, dict):
            return False
        if raw_event.get("t") or raw_event.get("type"):
            return True
        return bool(data.get("content") or data.get("text"))
