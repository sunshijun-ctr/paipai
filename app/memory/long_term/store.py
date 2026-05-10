"""Long-term memory store — persists user profile, preferences, and high-value conclusions across sessions."""
import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join(".", "data", "memory", "long_term.json")

_EMPTY: dict[str, Any] = {
    "user_profile": {
        "display_name": "研究者",
        "avatar": "",
        "self_description": "",
        "background": "",
        "research_directions": [],
        "long_term_topics": [],
    },
    "user_preferences": {
        "output_style": "",
        "communication_style": "",
        "task_patterns": [],
    },
    "conclusions": [],
    "updated_at": "",
}


class LongTermMemoryStore:
    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        self.load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            import copy
            self._data = copy.deepcopy(_EMPTY)
        except Exception as exc:
            logger.warning("Failed to load long-term memory: %s", exc)
            import copy
            self._data = copy.deepcopy(_EMPTY)

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._data["updated_at"] = datetime.now().isoformat()
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("Failed to save long-term memory: %s", exc)

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def user_profile(self) -> dict:
        return self._data.get("user_profile", {})

    @property
    def user_preferences(self) -> dict:
        return self._data.get("user_preferences", {})

    @property
    def conclusions(self) -> list[dict]:
        return self._data.get("conclusions", [])

    def is_empty(self) -> bool:
        profile = self.user_profile
        return (
            not profile.get("background")
            and not profile.get("research_directions")
            and not self.conclusions
        )

    # ── Mutation ──────────────────────────────────────────────────────────────

    def update_profile(self, updates: dict) -> None:
        profile = self._data.setdefault("user_profile", {})
        for key, value in updates.items():
            if isinstance(value, list):
                existing: list = profile.setdefault(key, [])
                for item in value:
                    if item and item not in existing:
                        existing.append(item)
            elif value:
                profile[key] = value

    def update_preferences(self, updates: dict) -> None:
        prefs = self._data.setdefault("user_preferences", {})
        for key, value in updates.items():
            if isinstance(value, list):
                existing = prefs.setdefault(key, [])
                for item in value:
                    if item and item not in existing:
                        existing.append(item)
            elif value:
                prefs[key] = value

    def add_conclusion(self, content: str, topic: str, session_id: str) -> None:
        if not content:
            return
        # Deduplicate by exact content match
        existing = [c["content"] for c in self._data.get("conclusions", [])]
        if content in existing:
            return
        self._data.setdefault("conclusions", []).append({
            "content": content,
            "topic": topic,
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
        })

    def get_profile_settings(self) -> dict[str, Any]:
        profile = self._data.setdefault("user_profile", {})
        return {
            "display_name": profile.get("display_name", "研究者"),
            "avatar": profile.get("avatar", ""),
            "self_description": profile.get("self_description", ""),
        }

    def update_profile_settings(
        self,
        display_name: str = "",
        avatar: str = "",
        self_description: str = "",
    ) -> dict[str, Any]:
        profile = self._data.setdefault("user_profile", {})
        profile["display_name"] = display_name.strip() or "研究者"
        profile["avatar"] = avatar.strip()
        profile["self_description"] = self_description.strip()
        return self.get_profile_settings()

    # ── Context string for agent injection ────────────────────────────────────

    def to_context_string(self) -> str:
        parts: list[str] = []
        profile_context = self.profile_to_context_string()
        if profile_context:
            parts.append(profile_context)

        prefs = self.user_preferences
        if prefs.get("output_style"):
            parts.append(f"Preferred output style: {prefs['output_style']}")
        if prefs.get("communication_style"):
            parts.append(f"Communication style: {prefs['communication_style']}")

        recent = self._data.get("conclusions", [])[-3:]
        for c in recent:
            parts.append(f"Past insight [{c.get('topic', '?')}]: {c['content']}")

        return "\n".join(parts)

    def profile_to_context_string(self) -> str:
        parts: list[str] = []
        profile = self.user_profile
        if profile.get("display_name"):
            parts.append(f"User display name: {profile['display_name']}")
        if profile.get("self_description"):
            parts.append(f"User self-description: {profile['self_description']}")
        if profile.get("background"):
            parts.append(f"User background: {profile['background']}")
        if profile.get("research_directions"):
            parts.append(f"Research directions: {', '.join(profile['research_directions'])}")
        if profile.get("long_term_topics"):
            parts.append(f"Long-term interests: {', '.join(profile['long_term_topics'])}")
        return "\n".join(parts)
