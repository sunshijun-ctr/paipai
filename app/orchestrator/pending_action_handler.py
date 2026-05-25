import re
from typing import Awaitable, Callable

from app.agents.base.agent import BaseAgent
from app.memory.manager import MemoryManager
from app.orchestrator.actions import run_note_action
from app.schemas.agent import AgentInput
from app.state.task_state import TaskState
from app.workflows.executor import run_agent_workflow

ProgressCallback = Callable[[str, str, int | None], Awaitable[None]]
BuildAgentInput = Callable[[str, str, TaskState], AgentInput]


async def handle_pending_action(
    *,
    state: TaskState,
    user_query: str,
    pending: dict,
    memory_manager: MemoryManager,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
) -> TaskState | None:
    action_type = pending.get("type")
    if action_type == "download_choice":
        return await _continue_download_choice(
            state=state,
            user_query=user_query,
            pending=pending,
            memory_manager=memory_manager,
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        )
    if action_type == "save_note_choice":
        return await _continue_save_note_choice(
            state=state,
            user_query=user_query,
            pending=pending,
            memory_manager=memory_manager,
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        )
    memory_manager.short_term.pending_action = None
    return None


async def _continue_download_choice(
    *,
    state: TaskState,
    user_query: str,
    pending: dict,
    memory_manager: MemoryManager,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
) -> TaskState | None:
    if _is_negative_confirmation(user_query):
        return _finish_pending_with_reply(state, memory_manager, "好的，本次不下载论文。")

    indices = _parse_requested_indices(user_query)
    if not indices and _looks_like_new_task(user_query):
        return None

    if not indices and not _is_download_affirmative_confirmation(user_query):
        return None

    papers = pending.get("options") or memory_manager.short_term.found_papers
    selected = _resolve_download_target(papers, indices, "")
    if not selected:
        return _finish_pending_with_reply(
            state,
            memory_manager,
            "没有找到要下载的论文，请回复例如：下载第1篇、下载前3篇。",
            clear_pending=False,
        )

    await progress("pending_download", "Downloading selected papers...", 30)
    state.record_agent_output(
        "intent_agent",
        {"result": {"user_intent": "paper_download", "workflow": "paper_search_workflow"}},
    )
    state.working_memory["pre_selected_papers"] = selected
    memory_manager.short_term.pending_action = None
    state.pending_action = None
    return await run_agent_workflow(
        workflow_name="paper_search_workflow",
        task_state=state,
        user_query=user_query,
        agent_names=["literature_agent"],
        agents=agents,
        build_agent_input=build_agent_input,
        progress=progress,
    )


async def _continue_save_note_choice(
    *,
    state: TaskState,
    user_query: str,
    pending: dict,
    memory_manager: MemoryManager,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
) -> TaskState | None:
    if _is_negative_confirmation(user_query):
        return _finish_pending_with_reply(state, memory_manager, "好的，本次总结不保存到笔记。")
    if not _is_affirmative_confirmation(user_query):
        return None

    summary_text = pending.get("summary_text", "")
    await progress("pending_note", "Saving summary to note...", 30)
    state.record_agent_output(
        "intent_agent",
        {"result": {"user_intent": "create_note_from_summary", "workflow": "note_action"}},
    )
    state.record_agent_output("summary_agent", {"result": {"final_report": summary_text}})
    state.working_memory["pending_summary_text"] = summary_text
    memory_manager.short_term.pending_action = None
    state.pending_action = None
    # note_workflow was demoted to a direct action handler.
    return await run_note_action(state, agents, build_agent_input, progress)


def _finish_pending_with_reply(
    state: TaskState,
    memory_manager: MemoryManager,
    reply: str,
    *,
    clear_pending: bool = True,
) -> TaskState:
    if clear_pending:
        memory_manager.short_term.pending_action = None
        state.pending_action = None
    else:
        state.pending_action = memory_manager.short_term.pending_action
    state.record_agent_output("library_agent", {"result": {"reply": reply}})
    return state


def _resolve_download_target(
    found_papers: list[dict],
    target_indices: list[int],
    target_keywords: str,
) -> list[dict]:
    if not found_papers:
        return []
    if target_indices:
        result = []
        for index in target_indices:
            if 1 <= index <= len(found_papers):
                result.append(found_papers[index - 1])
        return result
    if target_keywords:
        keyword = target_keywords.lower()
        return [p for p in found_papers if keyword in str(p.get("title", "")).lower()]
    return found_papers


def _parse_requested_indices(text: str) -> list[int]:
    query = text.lower().strip()
    if not query:
        return []

    first_n_patterns = (
        r"(?:下载|下|选择|选)?\s*前\s*(\d+)\s*(?:篇|个|条)?",
        r"(?:download|select|choose)?\s*top\s*(\d+)\s*(?:papers?)?",
    )
    for pattern in first_n_patterns:
        match = re.search(pattern, query)
        if match:
            return list(range(1, int(match.group(1)) + 1))

    indices: list[int] = []
    index_patterns = (
        r"第\s*(\d+)\s*(?:篇|个|条)?",
        r"(?:下载|下|选择|选|要)\s*(\d+)\s*(?:篇|个|条)?",
        r"(?:download|select|choose|paper)\s*(\d+)\s*(?:papers?)?",
    )
    for pattern in index_patterns:
        indices.extend(int(x) for x in re.findall(pattern, query))

    if indices:
        return _dedupe_preserve_order(indices)

    if re.fullmatch(r"\s*\d+(?:\s*(?:[,，、\s]|\band\b)\s*\d+)*\s*", query):
        return [int(x) for x in re.findall(r"\d+", query)]

    return []


def _dedupe_preserve_order(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _looks_like_new_task(text: str) -> bool:
    query = text.lower().strip()
    if not query:
        return False
    markers = (
        "搜索",
        "查找",
        "检索",
        "关于",
        "论文",
        "文章",
        "文献",
        "写",
        "总结",
        "解释",
        "加入知识库",
        "上传",
        "search",
        "find",
        "paper",
        "article",
        "write",
        "summarize",
        "about",
        "question",
    )
    return any(marker in query for marker in markers)


def _is_download_affirmative_confirmation(text: str) -> bool:
    query = text.lower().strip()
    return any(
        marker in query
        for marker in (
            "是",
            "好",
            "可以",
            "确认",
            "下载",
            "yes",
            "ok",
            "sure",
            "download",
        )
    )


def _is_affirmative_confirmation(text: str) -> bool:
    query = text.lower().strip()
    return any(
        marker in query
        for marker in (
            "是",
            "好",
            "可以",
            "确认",
            "下载",
            "保存",
            "yes",
            "ok",
            "sure",
            "download",
            "save",
        )
    )


def _is_negative_confirmation(text: str) -> bool:
    query = text.lower().strip()
    return any(
        marker in query
        for marker in (
            "不",
            "算了",
            "取消",
            "no",
            "cancel",
            "skip",
        )
    )
