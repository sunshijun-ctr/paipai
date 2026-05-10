import logging
from abc import ABC, abstractmethod
from typing import ClassVar

from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    _registry: ClassVar[dict[str, type["BaseAgent"]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name:
            BaseAgent._registry[cls.name] = cls
            logger.debug("Registered agent: %s", cls.name)

    @classmethod
    def get(cls, name: str) -> type["BaseAgent"]:
        if name not in cls._registry:
            available = list(cls._registry)
            raise KeyError(f"Agent '{name}' not registered. Available: {available}")
        return cls._registry[name]

    @classmethod
    def list_registered(cls) -> list[str]:
        return list(cls._registry.keys())

    @abstractmethod
    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        pass

    def _error_output(self, agent_input: AgentInput, error: str) -> AgentOutput:
        logger.error("[%s] error: %s", self.name, error)
        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.FAILED,
            errors=[error],
        )
