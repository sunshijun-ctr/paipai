from abc import ABC, abstractmethod

from app.schemas.agent_response import AgentResponse
from app.schemas.internal_message import InternalChatMessage


class ChannelInterface(ABC):
    @abstractmethod
    async def handle_message(self, message: InternalChatMessage) -> AgentResponse:
        """Process a normalized channel message and return an agent response."""

    @abstractmethod
    async def send_response(
        self,
        message: InternalChatMessage,
        response: AgentResponse,
    ) -> None:
        """Send an agent response back to the source platform."""
