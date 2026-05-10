import json
import logging
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
        if not source_content and task_type == "create_note_from_chat":
            turns = agent_input.input_data.get("conversation_history", [])
            source_content = "\n\n".join(f"{m.get('role')}: {m.get('content')}" for m in turns[-20:])

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
                return raw
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
