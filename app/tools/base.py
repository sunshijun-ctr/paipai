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
    # Alias → canonical name. Looking up an alias resolves to the same tool
    # instance as its canonical name. Used to expose the same tool under
    # multiple names (e.g. "search_papers" and "paper_search").
    _aliases: ClassVar[dict[str, str]] = {}

    @classmethod
    def register(cls, tool: BaseTool) -> None:
        cls._tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)

    @classmethod
    def register_alias(cls, alias: str, canonical: str) -> None:
        """Register *alias* as an alternate name for an already-registered tool.

        Both ``get(alias)`` and ``get(canonical)`` will resolve to the same
        instance. Useful when refactoring tool names without breaking callers
        that still use the old name."""
        if canonical not in cls._tools:
            raise KeyError(
                f"Cannot alias '{alias}' to unknown canonical tool '{canonical}'. "
                f"Available: {list(cls._tools)}"
            )
        cls._aliases[alias] = canonical
        logger.debug("Registered tool alias: %s → %s", alias, canonical)

    @classmethod
    def get(cls, name: str) -> BaseTool:
        # Resolve aliases first.
        resolved = cls._aliases.get(name, name)
        if resolved not in cls._tools:
            raise KeyError(
                f"Tool '{name}' not registered. Available: {list(cls._tools)} "
                f"(aliases: {list(cls._aliases)})"
            )
        return cls._tools[resolved]

    @classmethod
    def list_tools(cls) -> list[dict[str, Any]]:
        return [t.to_mcp_schema() for t in cls._tools.values()]

    @classmethod
    def list_names(cls) -> list[str]:
        """Canonical names only (does not include aliases)."""
        return list(cls._tools.keys())

    @classmethod
    def list_all_names(cls) -> list[str]:
        """Canonical names + aliases — useful when displaying the full
        addressable tool surface to a tool-using agent."""
        return list(cls._tools.keys()) + list(cls._aliases.keys())
