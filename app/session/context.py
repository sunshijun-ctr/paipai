from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionContext:
    session_id: str
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    current_task: str = ""
    last_workflow: str = ""
    last_workflow_output: str = ""
    active_entities: list[str] = field(default_factory=list)
    history_summary: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, session_id: str = "") -> "SessionContext":
        raw = data or {}
        return cls(
            session_id=str(raw.get("session_id") or session_id),
            recent_turns=list(raw.get("recent_turns") or []),
            current_task=str(raw.get("current_task") or ""),
            last_workflow=str(raw.get("last_workflow") or ""),
            last_workflow_output=str(raw.get("last_workflow_output") or ""),
            active_entities=[str(item) for item in raw.get("active_entities") or [] if str(item).strip()],
            history_summary=str(raw.get("history_summary") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "recent_turns": self.recent_turns,
            "current_task": self.current_task,
            "last_workflow": self.last_workflow,
            "last_workflow_output": self.last_workflow_output,
            "active_entities": self.active_entities,
            "history_summary": self.history_summary,
        }

    def merge_active_entities(self, entities: list[str], max_entities: int = 20) -> None:
        merged = list(self.active_entities)
        seen = {item.lower() for item in merged}
        for entity in entities:
            clean = str(entity).strip()
            if not clean or clean.lower() in seen:
                continue
            merged.append(clean)
            seen.add(clean.lower())
        self.active_entities = merged[-max_entities:]
