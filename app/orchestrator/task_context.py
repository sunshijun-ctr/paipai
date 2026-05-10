import time
import uuid
from typing import Any

from app.state.task_state import TaskState


CONTINUE_MARKERS = [
    "继续",
    "接着",
    "写多点",
    "写长一点",
    "写的详细一点",
    "写的再详细一点",
    "详细一点",
    "太短",
    "改一下",
    "修改",
    "写成",
    "写为",
    "整理成",
    "成一段",
    "一段话",
    "换一种说法",
    "按照上面",
    "基于刚才",
    "再生成",
    "再来",
    "上一版",
    "上面这段",
    "刚才那段",
    "这段",
    "重新写",
    "润色一下",
    "扩写一下",
    "再扩写",
    "继续扩写",
    "继续写",
    "continue",
    "keep going",
    "make it longer",
    "expand it",
    "revise it",
    "rewrite it",
    "polish it",
    "previous draft",
]

SWITCH_MARKERS = [
    "换个主题",
    "新任务",
    "重新开始",
    "不要管刚才",
    "先不管",
    "另一个问题",
    "新的问题",
    "new task",
    "different topic",
    "start over",
]


def empty_task_context() -> dict[str, Any]:
    return {
        "task_id": f"ctx_{uuid.uuid4().hex[:8]}",
        "task_type": "",
        "status": "active",
        "user_goal": "",
        "current_intent": "",
        "subject_type": "",
        "subject_title": "",
        "subject_content": "",
        "source_mode": "",
        "active_files": [],
        "active_library_titles": [],
        "retrieved_chunks": [],
        "constraints": {},
        "last_workflow": "",
        "last_stage": "",
        "last_agent_outputs_summary": {},
        "pending_next_action": "",
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def detect_task_relation(user_message: str, ctx: dict[str, Any] | None) -> str:
    if not ctx or ctx.get("status") != "active":
        return "new_task"
    q = (user_message or "").lower()
    if any(marker in q for marker in SWITCH_MARKERS):
        return "switch_task"
    if any(marker in q for marker in CONTINUE_MARKERS):
        return "continue_task"
    return "new_task"


def apply_task_context_to_state(state: TaskState, ctx: dict[str, Any] | None, relation: str) -> None:
    if not ctx:
        return
    state.working_memory["active_task_context"] = ctx
    state.working_memory["task_relation"] = relation
    if relation != "continue_task":
        return

    subject_content = str(ctx.get("subject_content") or "").strip()
    if subject_content and not state.working_memory.get("current_writing_context"):
        if ctx.get("task_type") == "academic_writing":
            state.working_memory["current_writing_context"] = {
                "title": ctx.get("subject_title", ""),
                "task_type": ctx.get("current_intent", ""),
                "content": subject_content,
                "citations": [],
                "material_usage_summary": "",
            }

    if ctx.get("retrieved_chunks"):
        state.working_memory.setdefault("task_context_retrieved_chunks", ctx.get("retrieved_chunks", []))
    if ctx.get("constraints"):
        state.working_memory.setdefault("task_context_constraints", ctx.get("constraints", {}))


def repair_task_context_from_history(
    ctx: dict[str, Any] | None,
    recent_turns: list[dict] | None,
) -> dict[str, Any]:
    """Recover the last useful assistant draft if the active context was poisoned.

    A failed follow-up can produce generic text such as "no material was provided".
    If that generic output is saved as the active subject, the next follow-up keeps
    editing the wrong text. For continuation tasks, prefer the latest substantive
    assistant reply from recent history when the stored subject looks unusable.
    """
    if not ctx:
        return {}
    repaired = dict(ctx)
    subject = str(repaired.get("subject_content") or "").strip()
    if subject and not looks_like_missing_material_reply(subject):
        return repaired

    for turn in reversed(recent_turns or []):
        if turn.get("role") != "assistant":
            continue
        content = str(turn.get("content") or "").strip()
        if is_substantive_context_content(content):
            repaired["subject_content"] = content[:12000]
            repaired["subject_type"] = repaired.get("subject_type") or "assistant_reply"
            repaired["subject_title"] = repaired.get("subject_title") or ""
            return repaired
    return repaired


def looks_like_missing_material_reply(text: str) -> bool:
    q = (text or "").strip()
    if not q:
        return True
    markers = [
        "未提供具体",
        "没有提供具体",
        "缺乏具体",
        "无法直接生成包含引用",
        "无法直接引用相关研究",
        "没有使用任何提供的材料",
        "无可用材料",
        "No material",
        "no material",
        "not provided",
    ]
    return any(marker in q for marker in markers)


def is_substantive_context_content(text: str) -> bool:
    q = (text or "").strip()
    if len(q) < 80:
        return False
    if looks_like_missing_material_reply(q):
        return False
    return True


def build_contextual_intent_query(user_message: str, ctx: dict[str, Any] | None, relation: str) -> str:
    if relation != "continue_task" or not ctx:
        return user_message
    subject = str(ctx.get("subject_content") or "")[:1500]
    return (
        "[Task Context: continue the active task]\n"
        f"Task type: {ctx.get('task_type', '')}\n"
        f"Subject title: {ctx.get('subject_title', '')}\n"
        f"Last workflow: {ctx.get('last_workflow', '')}\n"
        f"Subject content excerpt:\n{subject}\n\n"
        f"User message:\n{user_message}"
    )


def continuation_workflow(ctx: dict[str, Any] | None, user_message: str = "") -> str:
    if not ctx:
        return ""
    q = (user_message or "").lower()
    writing_markers = [
        "写成",
        "写为",
        "改写",
        "润色",
        "扩写",
        "写多点",
        "写长一点",
        "换一种说法",
        "整理成",
        "成一段",
        "paraphrase",
        "rewrite",
        "polish",
        "expand",
    ]
    if ctx.get("subject_content") and any(marker in q for marker in writing_markers):
        return "academic_writing_workflow"
    task_type = ctx.get("task_type", "")
    if task_type == "academic_writing":
        return "academic_writing_workflow"
    if task_type in {"library_qa", "paper_reading"}:
        return "question_answer_workflow"
    if task_type == "paper_search":
        return "paper_search_workflow"
    if task_type == "note":
        return "note_workflow"
    return ctx.get("last_workflow", "")


def build_task_context_from_state(state: TaskState, assistant_reply: str = "") -> dict[str, Any]:
    previous = state.working_memory.get("active_task_context") or {}
    ctx = {**empty_task_context(), **previous}
    intent = state.agent_outputs.get("intent_agent", {}).get("result", {})
    workflow = state.workflow or intent.get("workflow") or ""
    user_intent = intent.get("user_intent") or intent.get("intent") or ""

    ctx["status"] = "active"
    ctx["user_goal"] = state.user_goal
    ctx["current_intent"] = user_intent
    ctx["last_workflow"] = workflow
    ctx["last_stage"] = state.current_stage
    ctx["updated_at"] = time.time()
    ctx["constraints"] = intent.get("constraints") or ctx.get("constraints") or {}
    ctx["source_mode"] = state.working_memory.get("writing_source", ctx.get("source_mode", ""))

    if workflow == "academic_writing_workflow" or "writing_agent" in state.agent_outputs:
        _fill_writing_context(ctx, state, assistant_reply)
    elif workflow == "web_search_workflow" or "web_agent" in state.agent_outputs:
        _fill_web_context(ctx, state, assistant_reply)
    elif workflow == "question_answer_workflow" or "reading_agent" in state.agent_outputs:
        _fill_reading_context(ctx, state, assistant_reply)
    elif workflow == "paper_search_workflow" or "literature_agent" in state.agent_outputs:
        _fill_search_context(ctx, state, assistant_reply)
    elif workflow == "note_workflow" or "note_agent" in state.agent_outputs:
        ctx["task_type"] = "note"
        ctx["subject_type"] = "note"
        ctx["subject_content"] = assistant_reply[:6000]
    else:
        ctx["task_type"] = ctx.get("task_type") or "chat"
        ctx["subject_type"] = ctx.get("subject_type") or "assistant_reply"
        ctx["subject_content"] = assistant_reply[:6000]

    ctx["last_agent_outputs_summary"] = _summarize_agent_outputs(state)
    return ctx


def _fill_writing_context(ctx: dict[str, Any], state: TaskState, assistant_reply: str) -> None:
    result = state.agent_outputs.get("writing_agent", {}).get("result", {})
    content = str(result.get("content") or assistant_reply or "").strip()
    ctx["task_type"] = "academic_writing"
    ctx["subject_type"] = "writing_output"
    ctx["subject_title"] = result.get("title") or ctx.get("subject_title", "")
    ctx["subject_content"] = content[:12000]
    ctx["retrieved_chunks"] = state.working_memory.get("writing_material_chunks", [])[:8]


def _fill_reading_context(ctx: dict[str, Any], state: TaskState, assistant_reply: str) -> None:
    result = state.agent_outputs.get("reading_agent", {}).get("result", {})
    notes = result.get("reading_notes") or []
    content = assistant_reply
    title = ""
    if notes:
        title = notes[0].get("title", "")
        content = notes[0].get("answer", "") or assistant_reply
    ctx["task_type"] = "library_qa" if state.working_memory.get("library_qa_mode") else "paper_reading"
    ctx["subject_type"] = "reading_answer"
    ctx["subject_title"] = title or ctx.get("subject_title", "")
    ctx["subject_content"] = str(content)[:12000]
    retrieval = state.agent_outputs.get("retrieval_agent", {}).get("result", {})
    ctx["retrieved_chunks"] = retrieval.get("retrieved_chunks", [])[:8]
    active_title = retrieval.get("active_title") or result.get("metadata", {}).get("active_title")
    ctx["active_library_titles"] = [active_title] if active_title else ctx.get("active_library_titles", [])


def _fill_web_context(ctx: dict[str, Any], state: TaskState, assistant_reply: str) -> None:
    result = state.agent_outputs.get("web_agent", {}).get("result", {})
    notes = result.get("web_notes") or []
    content = assistant_reply
    title = "网页搜索结果"
    if notes:
        title = notes[0].get("title", title)
        content = notes[0].get("answer", "") or assistant_reply
    ctx["task_type"] = "web_search"
    ctx["subject_type"] = "web_answer"
    ctx["subject_title"] = title
    ctx["subject_content"] = str(content)[:12000]
    ctx["source_refs"] = result.get("metadata", {}).get("urls", [])


def _fill_search_context(ctx: dict[str, Any], state: TaskState, assistant_reply: str) -> None:
    result = state.agent_outputs.get("literature_agent", {}).get("result", {})
    papers = result.get("selected_papers") or result.get("papers") or []
    titles = [p.get("title", "") for p in papers[:10] if p.get("title")]
    ctx["task_type"] = "paper_search"
    ctx["subject_type"] = "paper_list"
    ctx["subject_title"] = titles[0] if titles else ""
    ctx["subject_content"] = "\n".join(f"- {title}" for title in titles) or assistant_reply[:6000]


def _summarize_agent_outputs(state: TaskState) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, output in state.agent_outputs.items():
        result = output.get("result", {}) if isinstance(output, dict) else {}
        if not result:
            continue
        summary[name] = {
            "status": output.get("status"),
            "keys": list(result.keys())[:12],
        }
    return summary
