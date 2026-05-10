import asyncio
import logging

from app.memory.manager import MemoryManager
from app.orchestrator.state_manager import sync_memory_from_task_state
from app.schemas.agent_response import AgentResponse
from app.schemas.internal_message import InternalChatMessage

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self, orchestrator, timeout_seconds: float = 50.0) -> None:
        self.orchestrator = orchestrator
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[str, MemoryManager] = {}

    async def chat(self, message: InternalChatMessage) -> AgentResponse:
        if not message.text.strip() and not message.attachments:
            return AgentResponse(text="请发送文字问题，我会尽力回答。", success=False)

        mem = self._get_memory(message.session_id)
        try:
            state = await asyncio.wait_for(
                self.orchestrator.run(
                    message.text,
                    session_id=message.session_id,
                    conversation_history=mem.short_term.get_full_history(),
                    stored_papers=mem.short_term.stored_papers,
                    found_papers=mem.short_term.found_papers,
                    memory_manager=mem,
                ),
                timeout=self.timeout_seconds,
            )
            reply = collect_agent_reply(state)
            if not reply and state.errors:
                reply = f"处理这条消息时遇到问题：{state.errors[-1]}"
            if not reply:
                reply = "我暂时没有生成有效回复，请换一种问法再试一次。"
            sync_memory_from_task_state(
                state=state,
                user_text=message.text,
                assistant_reply=reply,
                memory_manager=mem,
            )
            mem.save()
            return AgentResponse(
                text=reply,
                success=not bool(state.errors),
                metadata={
                    "intent": state.agent_outputs.get("intent_agent", {})
                    .get("result", {})
                    .get("user_intent", ""),
                    "errors": state.errors,
                },
            )
        except asyncio.TimeoutError:
            logger.warning("Agent timeout for QQ session %s", message.session_id)
            return AgentResponse(
                text="当前任务处理时间较长，请稍后重试，或到 Web 端继续这个任务。",
                success=False,
                metadata={"error": "timeout"},
            )
        except Exception as exc:
            logger.exception("Agent failed for QQ session %s", message.session_id)
            return AgentResponse(
                text="我刚才处理这条消息时失败了。你可以换一种问法，或者到 Web 端继续这个任务。",
                success=False,
                metadata={"error": str(exc)},
            )

    def _get_memory(self, session_id: str) -> MemoryManager:
        if session_id not in self._sessions:
            self._sessions[session_id] = MemoryManager(
                session_id=session_id,
                llm=self.orchestrator.llm,
            )
        mem = self._sessions[session_id]
        mem.set_llm(self.orchestrator.llm)
        return mem


def collect_agent_reply(state) -> str:
    if lib := state.agent_outputs.get("library_agent", {}):
        return lib.get("result", {}).get("reply", "")
    if writing := state.agent_outputs.get("writing_agent", {}):
        result = writing.get("result", {})
        return result.get("content") or result.get("reply") or ""
    if chat := state.agent_outputs.get("chat_agent", {}):
        return chat.get("result", {}).get("reply", "")
    if read := state.agent_outputs.get("reading_agent", {}):
        notes = read.get("result", {}).get("reading_notes", [])
        return "\n\n---\n\n".join(
            f"**{item.get('title', '')}**\n\n{item.get('answer', '')}"
            for item in notes
        )
    if summary := state.agent_outputs.get("summary_agent", {}):
        return summary.get("result", {}).get("final_report", "")
    if note := state.agent_outputs.get("note_agent", {}):
        return note.get("result", {}).get("reply", "")

    lit = state.agent_outputs.get("literature_agent", {})
    if lit:
        result = lit.get("result", {})
        papers = result.get("selected_papers", [])
        if papers:
            return format_paper_search_reply(papers)
        downloaded = lit.get("artifacts", {}).get("downloaded_pdfs", [])
        if downloaded:
            return f"已成功下载 {len(downloaded)} 篇论文，现在可以直接提问。"
    return ""


def format_paper_search_reply(papers: list[dict]) -> str:
    lines = [
        f"找到 **{len(papers)}** 篇相关论文：",
        "",
    ]
    for idx, paper in enumerate(papers, 1):
        title = str(paper.get("title") or "Untitled").strip()
        year = paper.get("year")
        citations = paper.get("citations")
        meta = []
        if year:
            meta.append(str(year))
        if citations:
            meta.append(f"{citations} citations")
        suffix = f" ({', '.join(meta)})" if meta else ""
        lines.append(f"{idx}. {title}{suffix}")
    lines.extend([
        "",
        "可以继续回复编号让我下载或阅读，例如：下载第 1 篇、阅读 1 3 5、下载前 3 篇。",
    ])
    return "\n".join(lines)
