import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.schemas.tool import ToolResult

logger = logging.getLogger(__name__)


class BaseTool(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def to_mcp_schema(self) -> dict[str, Any]:
        """Return MCP-compatible tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        pass


class ToolRegistry:
    _tools: ClassVar[dict[str, BaseTool]] = {}

    @classmethod
    def register(cls, tool: BaseTool) -> None:
        cls._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    @classmethod
    def get(cls, name: str) -> BaseTool:
        if name not in cls._tools:
            raise KeyError(f"Tool '{name}' not registered. Available: {list(cls._tools)}")
        return cls._tools[name]

    @classmethod
    def list_tools(cls) -> list[dict[str, Any]]:
        return [t.to_mcp_schema() for t in cls._tools.values()]

    @classmethod
    def list_names(cls) -> list[str]:
        return list(cls._tools.keys())
