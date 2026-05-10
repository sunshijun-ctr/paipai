import asyncio
import unittest

from app.channels.qq.qq_config import QQConfig
from app.channels.qq.qq_deduplicator import InMemoryDeduplicator
from app.channels.qq.qq_event_receiver import QQEventReceiver
from app.channels.qq.qq_message_adapter import QQMessageAdapter
from app.channels.qq.qq_sender import format_for_qq
from app.channels.qq.qq_signature import build_webhook_validation_response
from app.schemas.agent_response import AgentResponse


class MockAgentService:
    async def chat(self, message):
        return AgentResponse(text=f"reply:{message.text}", metadata={"sid": message.session_id})


class QQChannelTests(unittest.TestCase):
    def test_private_message_adapter(self):
        event = {
            "id": "event_1",
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "msg_1",
                "content": "你好",
                "author": {"user_openid": "user_1"},
            },
        }
        msg = QQMessageAdapter().to_internal(event)
        self.assertEqual(msg.session_id, "qq_private_user_1")
        self.assertEqual(msg.conversation_type, "private")
        self.assertEqual(msg.text, "你好")

    def test_group_message_adapter_removes_mention(self):
        event = {
            "id": "event_2",
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "msg_2",
                "group_openid": "group_1",
                "content": "<@bot_1> 解释 RAG",
                "author": {"member_openid": "member_1"},
                "mentions": [{"id": "bot_1"}],
            },
        }
        msg = QQMessageAdapter().to_internal(event)
        self.assertEqual(msg.session_id, "qq_group_group_1_member_1")
        self.assertEqual(msg.conversation_type, "group")
        self.assertEqual(msg.text, "解释 RAG")

    def test_channel_message_adapter(self):
        event = {
            "id": "event_3",
            "t": "AT_MESSAGE_CREATE",
            "d": {
                "id": "msg_3",
                "guild_id": "guild_1",
                "channel_id": "channel_1",
                "content": "<@bot_1> hello",
                "author": {"id": "user_2"},
            },
        }
        msg = QQMessageAdapter().to_internal(event)
        self.assertEqual(msg.session_id, "qq_guild_guild_1_channel_1_user_2")
        self.assertEqual(msg.conversation_type, "guild")

    def test_deduplicator(self):
        dedup = InMemoryDeduplicator()
        self.assertFalse(dedup.exists("msg"))
        dedup.mark("msg")
        self.assertTrue(dedup.exists("msg"))

    def test_format_for_qq_truncates(self):
        text = format_for_qq("a" * 100, max_len=60)
        self.assertLessEqual(len(text), 60)
        self.assertIn("已截断", text)

    def test_event_receiver_drops_duplicate(self):
        event = {
            "id": "event_4",
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "msg_4",
                "content": "你好",
                "author": {"user_openid": "user_1"},
            },
        }
        receiver = QQEventReceiver(
            agent_service=MockAgentService(),
            config=QQConfig(QQ_ENABLE_MESSAGE_DEDUP=True),
        )

        async def run():
            first = await receiver.handle_qq_event(event)
            second = await receiver.handle_qq_event(event)
            return first, second

        first, second = asyncio.run(run())
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])

    def test_webhook_validation_response(self):
        result = build_webhook_validation_response(
            {"op": 13, "d": {"plain_token": "token", "event_ts": "123"}},
            QQConfig(QQ_BOT_SECRET="abcdefghijklmnopqrstuvwxyz123456"),
        )
        self.assertEqual(result["plain_token"], "token")
        self.assertTrue(result["signature"])

    def test_unknown_webhook_event_is_acked(self):
        receiver = QQEventReceiver(
            agent_service=MockAgentService(),
            config=QQConfig(QQ_ENABLE_MESSAGE_DEDUP=True),
        )

        async def run():
            return await receiver.handle_webhook({"op": 99, "d": {"hello": "world"}})

        result = asyncio.run(run())
        self.assertTrue(result["success"])
        self.assertTrue(result["ignored"])


if __name__ == "__main__":
    unittest.main()
