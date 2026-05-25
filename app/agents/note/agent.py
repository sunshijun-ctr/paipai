import json
import logging
import re
from typing import Any

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.schemas.note_schema import NoteCreate, NoteUpdate
from app.services.llm import BaseLLMProvider, LLMMessage
from app.services.note_service import get_note_service
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)

_NOTE_SYSTEM = """\
You are the Note Agent in a research assistant.
Turn user conversations, paper-reading outputs, and research ideas into concise, useful Markdown notes.

Rules:
- Only create or modify notes when the user explicitly asks.
- Do not request embedding unless the user explicitly asks to vectorize/embed/add the note to the vector store.
- The JSON field `source_content` is the authoritative material for the note body.
- For create_note_from_summary, preserve the supplied summary faithfully. Do not replace it with the user's confirmation text, do not invent new facts, and do not use unrelated conversation history.
- If source_content is present, the note content must be based on source_content, not on user_instruction.
- Return ONLY strict JSON, no markdown fences.

JSON shape:
{
  "action": "create_note | update_note | delete_note | search_note | embed_note | none",
  "note": {
    "id": "",
    "title": "",
    "content_markdown": "",
    "source_type": "manual | conversation | reading | summary",
    "source_id": "",
    "paper_id": "",
    "tags": []
  },
  "query": "",
  "message_to_user": ""
}
"""


class NoteAgent(BaseAgent):
    name = "note_agent"
    description = "Creates, organizes, searches, updates, deletes, and embeds research notes."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        svc = get_note_service()
        data = agent_input.input_data
        task_type = data.get("task_type", "create_note")
        user_id = data.get("user_id", "local")

        try:
            if task_type in {"list_notes", "search_note"}:
                query = data.get("query", "")
                notes = svc.search_notes_by_metadata(user_id, query) if query else svc.list_notes(user_id)
                return self._ok(agent_input, {"action": "search_note", "notes": [n.model_dump() for n in notes],
                                              "reply": _format_note_list(notes)})

            if task_type in {"embed_note", "reembed_note"}:
                note = _resolve_note(svc, data)
                result = await svc.reembed_note(note.id) if task_type == "reembed_note" else await svc.embed_note(note.id)
                library_chunks = result.get("library_chunks_indexed", 0)
                return self._ok(agent_input, {"action": task_type, "note": note.model_dump(), **result,
                                              "reply": f"已向量化笔记《{note.title}》，写入 {result.get('chunks_indexed', 0)} 个片段，并同步到知识库 {library_chunks} 个片段。"})

            if task_type == "delete_note":
                note = _resolve_note(svc, data)
                svc.delete_note(note.id)
                return self._ok(agent_input, {"action": "delete_note", "note_id": note.id,
                                              "reply": f"已删除笔记《{note.title}》。"})

            if task_type == "update_note":
                note = _resolve_note(svc, data)
                draft = await self._draft_note(agent_input, state, task_type)
                note_data = draft.get("note", {})
                updated = svc.update_note(note.id, NoteUpdate(
                    title=note_data.get("title") or None,
                    content_markdown=note_data.get("content_markdown") or None,
                    source_type=note_data.get("source_type") or None,
                    source_id=note_data.get("source_id") or None,
                    paper_id=note_data.get("paper_id") or None,
                    tags=note_data.get("tags") or None,
                ))
                return self._ok(agent_input, {"action": "update_note", "note": updated.model_dump(),
                                              "reply": f"已更新笔记《{updated.title}》。"})

            draft = await self._draft_note(agent_input, state, task_type)
            note_data = draft.get("note", {})
            created = svc.create_note(NoteCreate(
                user_id=user_id,
                title=note_data.get("title") or _fallback_title(agent_input.user_goal),
                content_markdown=note_data.get("content_markdown") or data.get("content") or "",
                source_type=note_data.get("source_type") or _source_type_for(task_type),
                source_id=note_data.get("source_id") or data.get("source_id", ""),
                paper_id=note_data.get("paper_id") or data.get("paper_id", ""),
                conversation_id=agent_input.session_id,
                tags=note_data.get("tags") or [],
            ))
            reply = draft.get("message_to_user") or f"已创建笔记《{created.title}》。"
            return self._ok(agent_input, {"action": "create_note", "note": created.model_dump(), "reply": reply})
        except Exception as exc:
            logger.exception("NoteAgent failed")
            return self._error_output(agent_input, str(exc))

    async def _draft_note(self, agent_input: AgentInput, state: TaskState, task_type: str) -> dict[str, Any]:
        source_content = agent_input.input_data.get("source_content", "")
        if task_type == "create_note_from_summary" and source_content.strip():
            return _summary_draft(agent_input, source_content)
        if not source_content and task_type == "create_note_from_reading":
            read = state.agent_outputs.get("reading_agent", {})
            notes = read.get("result", {}).get("reading_notes", [])
            source_content = "\n\n".join(
                f"Paper: {n.get('title','')}\nQuestion: {n.get('question','')}\nAnswer: {n.get('answer','')}"
                for n in notes
            )
        if not source_content and _wants_conversation_source(agent_input.user_goal, task_type):
            turns = agent_input.input_data.get("conversation_history", [])
            source_content = _conversation_to_source(turns, agent_input.user_goal, task_type)
            logger.info(
                "NoteAgent conversation fallback: task_type=%s history_turns=%d source_chars=%d",
                task_type,
                len(turns),
                len(source_content),
            )

        prompt = {
            "task_type": task_type,
            "user_instruction": agent_input.user_goal,
            "source_content": source_content,
        }
        try:
            raw = await self.llm.complete_json(
                messages=[LLMMessage(role="user", content=json.dumps(prompt, ensure_ascii=False))],
                system=_NOTE_SYSTEM,
            )
            if isinstance(raw, dict):
                # Safety net: if the model returned an empty/refusal draft but we
                # actually have material, build the note from the material rather
                # than passing the refusal through. Only respect a refusal when we
                # genuinely have nothing to save.
                if _draft_has_content(raw) or not source_content.strip():
                    return raw
                logger.info("NoteAgent LLM returned empty draft despite source_content; using source fallback")
        except Exception as exc:
            logger.warning("NoteAgent LLM draft failed: %s", exc)

        return _fallback_draft(agent_input, source_content, task_type)

    def _ok(self, agent_input: AgentInput, result: dict[str, Any]) -> AgentOutput:
        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result=result,
            next_suggestion="continue_note_management",
        )


# Markers that indicate the user wants to capture the ongoing chat into a note,
# even when the intent was classified as a generic create_note.
_CHAT_REFERENCE_MARKERS = (
    "对话", "聊天", "刚才", "刚刚", "之前", "上面", "上述", "这段", "那段",
    "这个回答", "那个回答", "上一个回答", "上一条", "我们讨论", "我们聊",
    "对话内容", "聊天记录",
    "conversation", "chat", "above", "previous", "this answer", "what we discussed",
)
# A bare "save as note" command carries no content of its own, so it implicitly
# refers to the preceding assistant answer.
_SAVE_COMMAND_MARKERS = (
    "保存", "存为", "存成", "存到", "存起来", "记下", "记一下", "收藏", "存笔记",
    "save", "note it", "make a note", "save as note",
)
_WHOLE_CONVERSATION_MARKERS = ("整个", "全部", "所有", "完整", "整理", "whole", "entire", "all of")


_NOTE_FILLER_TOKENS = (
    *_SAVE_COMMAND_MARKERS,
    "笔记", "notes", "note", "为", "成", "到", "起来", "一下", "把", "请",
    "帮我", "帮", "这", "那", "个", "条", "下来", "记录", "the", "as", "a", "it",
)


# Task types that are, by definition, derived from the current session — if no
# explicit source material was supplied, the conversation IS the material.
_SESSION_DERIVED_TASK_TYPES = {"create_note_from_chat", "create_note_from_summary"}


def _draft_has_content(draft: dict[str, Any]) -> bool:
    """Did the LLM produce a usable note body (vs. a refusal/clarification)?"""
    note = draft.get("note") or {}
    return bool(str(note.get("content_markdown") or "").strip())


def _wants_conversation_source(user_goal: str, task_type: str) -> bool:
    """Should an empty-source note pull from the conversation?

    True whenever the task is explicitly session-derived (from_chat / from_summary),
    and also for a generic create_note when the user references the chat
    ("把刚才的回答保存为笔记") or issues a bare save command ("保存为笔记")
    that carries no content of its own.
    """
    if task_type in _SESSION_DERIVED_TASK_TYPES:
        return True
    if task_type not in {"create_note", "note", ""}:
        return False
    text = (user_goal or "").strip().lower()
    if any(marker in text for marker in _CHAT_REFERENCE_MARKERS):
        return True
    return _is_bare_save_command(text)


def _is_bare_save_command(text: str) -> bool:
    """A save instruction that carries no content of its own.

    "保存为笔记" / "存成笔记" → bare (implicitly saves the preceding answer).
    "记一下：明天买牛奶" → not bare (it has its own content after the verb).
    """
    t = (text or "").lower()
    if not any(marker in t for marker in _SAVE_COMMAND_MARKERS):
        return False
    residual = t
    for token in _NOTE_FILLER_TOKENS:
        residual = residual.replace(token, "")
    residual = re.sub(r"[\s:：,，。.!！?？、…\-_/（）()]+", "", residual)
    return len(residual) <= 2


def _conversation_to_source(turns: list[dict[str, Any]], user_goal: str = "", task_type: str = "") -> str:
    """Build note material from chat history.

    Defaults to the most recent assistant answer plus the user question that
    prompted it (a focused note). A summary note, or an explicit request for the
    whole conversation, uses the recent transcript instead.
    """
    cleaned = [m for m in (turns or []) if str(m.get("content") or "").strip()]
    if not cleaned:
        return ""

    wants_whole = (
        task_type == "create_note_from_summary"
        or any(marker in (user_goal or "").lower() for marker in _WHOLE_CONVERSATION_MARKERS)
    )
    if wants_whole:
        selected = cleaned[-20:]
    else:
        last_assistant = next(
            (i for i in range(len(cleaned) - 1, -1, -1) if cleaned[i].get("role") == "assistant"),
            None,
        )
        if last_assistant is None:
            selected = cleaned[-6:]
        else:
            selected = cleaned[max(0, last_assistant - 1): last_assistant + 1]

    return "\n\n".join(f"{m.get('role')}: {m.get('content')}" for m in selected)


def _resolve_note(svc, data: dict[str, Any]):
    note_id = data.get("note_id") or data.get("target_note_id")
    title = data.get("title") or data.get("target_title")
    if note_id:
        return svc.get_note(note_id)
    if title:
        matches = svc.search_notes_by_metadata(data.get("user_id", "local"), title)
        if matches:
            return matches[0]
    notes = svc.list_notes(data.get("user_id", "local"))
    if notes:
        return notes[0]
    raise ValueError("没有找到可操作的笔记，请指定笔记标题或先创建笔记。")


def _source_type_for(task_type: str) -> str:
    if task_type == "create_note_from_summary":
        return "summary"
    if task_type == "create_note_from_chat":
        return "conversation"
    if task_type == "create_note_from_reading":
        return "reading"
    if task_type == "summarize_session":
        return "summary"
    return "manual"


def _fallback_title(text: str) -> str:
    text = text.strip().replace("\n", " ")
    return text[:30] or "新笔记"


def _fallback_draft(agent_input: AgentInput, source_content: str, task_type: str) -> dict[str, Any]:
    title = _fallback_title(agent_input.user_goal)
    content = source_content.strip() or agent_input.input_data.get("content", "").strip()
    if not content:
        content = agent_input.user_goal
    return {
        "action": "create_note",
        "note": {
            "title": title,
            "content_markdown": f"# {title}\n\n{content}",
            "source_type": _source_type_for(task_type),
            "source_id": "",
            "paper_id": "",
            "tags": [],
        },
        "message_to_user": f"已创建笔记《{title}》。",
    }


def _summary_draft(agent_input: AgentInput, source_content: str) -> dict[str, Any]:
    title = _summary_title(source_content)
    content = source_content.strip()
    if not content.startswith("#"):
        content = f"# {title}\n\n{content}"
    return {
        "action": "create_note",
        "note": {
            "title": title,
            "content_markdown": content,
            "source_type": "summary",
            "source_id": "",
            "paper_id": "",
            "tags": ["summary"],
        },
        "message_to_user": f"已创建笔记《{title}》。",
    }


def _summary_title(source_content: str) -> str:
    for line in source_content.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:40]
    return "会话总结"


def _format_note_list(notes) -> str:
    if not notes:
        return "当前没有找到匹配的笔记。"
    lines = [f"找到 {len(notes)} 条笔记："]
    for n in notes[:20]:
        tags = f" [{', '.join(n.tags)}]" if n.tags else ""
        lines.append(f"- {n.title}{tags} · {n.embedding_status}")
    return "\n".join(lines)
