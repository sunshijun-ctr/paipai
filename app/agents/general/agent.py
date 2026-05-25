import logging
from typing import Any

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMMessage
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)

RECENT_HISTORY_TOKEN_LIMIT = 6000


_GENERAL_SYSTEM = """\
You are the General Agent of a research assistant.

You handle open-ended tasks with a Plan-and-Action style:
1. Understand the user's goal.
2. Make a brief plan.
3. Use available context from tools/workflows if it was already gathered.
4. Produce a useful final answer.

You may use existing workflow results as subtask evidence, but you are not a rigid workflow.
If the task is simple, answer directly with only a short implicit plan.
If the task is complex, show a concise plan first, then execute it in the answer.

Available context may include:
- literature_agent results: searched papers
- retrieval_agent results: knowledge-base chunks
- reading_agent results: answers from documents/library
- summary_agent results: conversation summaries
- note_agent results: note operations
- image_analysis_tool results: extracted image context

When using gathered context, synthesize it naturally. Do not mention implementation details unless helpful.
When the user asks for design advice, give tradeoffs and a concrete recommendation.
When information is missing, ask a focused clarification question only if execution would be risky.
Use the user's language.
"""


class GeneralAgent(BaseAgent):
    name = "general_agent"
    description = "Handles open-ended tasks with plan-and-action reasoning and available tool/workflow context."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        user_message: str = agent_input.input_data.get("user_message", agent_input.user_goal)
        history: list[dict] = agent_input.input_data.get("conversation_history", [])
        history_summary: str = agent_input.input_data.get("history_summary", "")
        current_task: str = agent_input.input_data.get("current_task", "")
        gathered_context = _build_gathered_context(state.agent_outputs, state.working_memory)
        intent_result = state.agent_outputs.get("intent_agent", {}).get("result", {})
        route_reason = intent_result.get("reason", "")

        state.update_stage("general_plan_action", self.name)

        messages = [
            LLMMessage(role=m["role"], content=m["content"])
            for m in _trim_history_by_token_estimate(history)
        ]
        context_parts = []
        if route_reason:
            context_parts.append(f"Routing reason: {route_reason}")
        if gathered_context:
            context_parts.append("Available workflow/tool context:\n" + gathered_context)
        context_parts.append("User request:\n" + user_message)
        messages.append(LLMMessage(role="user", content="\n\n".join(context_parts)))

        try:
            llm_resp = await self.llm.complete(
                messages=messages,
                system=_build_general_system(current_task, history_summary),
            )
            reply = llm_resp.content
        except Exception as exc:
            logger.warning("GeneralAgent LLM call failed: %s", exc)
            reply = _fallback_reply(user_message, gathered_context)

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result={
                "reply": reply,
                "mode": "plan_and_action",
                "used_context": bool(gathered_context),
            },
            next_suggestion="continue_or_dispatch_subworkflow",
        )


def _build_general_system(current_task: str = "", history_summary: str = "") -> str:
    parts: list[str] = []
    if current_task:
        parts.append(f"当前用户意图：{current_task}")
    parts.append(_GENERAL_SYSTEM)
    if history_summary:
        parts.append(f"背景信息：{history_summary}")
    return "\n\n".join(parts)


def _trim_history_by_token_estimate(
    history: list[dict],
    token_limit: int = RECENT_HISTORY_TOKEN_LIMIT,
) -> list[dict]:
    selected: list[dict] = []
    total_tokens = 0

    for message in reversed(history):
        content = str(message.get("content", ""))
        token_estimate = len(content) // 4
        if selected and total_tokens + token_estimate > token_limit:
            break
        selected.append(message)
        total_tokens += token_estimate

    selected.reverse()
    return selected


def _build_gathered_context(agent_outputs: dict[str, Any], working_memory: dict[str, Any]) -> str:
    parts: list[str] = []

    image_analysis = agent_outputs.get("image_analysis_tool", {})
    if image_analysis:
        result = image_analysis.get("result", {})
        image_context = result.get("context_for_agent") or working_memory.get("image_context") or ""
        if image_context:
            parts.append("Image analysis context:\n" + str(image_context)[:1600])

    literature = agent_outputs.get("literature_agent", {}).get("result", {})
    papers = literature.get("selected_papers") or []
    if papers:
        lines = []
        for i, paper in enumerate(papers[:8], 1):
            title = paper.get("title") or "Untitled"
            year = paper.get("year") or paper.get("published") or ""
            citations = paper.get("citation_count") or paper.get("citationCount") or ""
            meta = ", ".join(str(x) for x in [year, f"{citations} citations" if citations else ""] if x)
            lines.append(f"{i}. {title}" + (f" ({meta})" if meta else ""))
        parts.append("Papers found:\n" + "\n".join(lines))

    retrieval = agent_outputs.get("retrieval_agent", {}).get("result", {})
    contexts = retrieval.get("contexts") or []
    if contexts:
        parts.append("Knowledge-base passages:\n" + "\n\n".join(str(c)[:900] for c in contexts[:4]))

    reading = agent_outputs.get("reading_agent", {}).get("result", {})
    notes = reading.get("reading_notes") or []
    if notes:
        rendered = []
        for note in notes[:4]:
            title = note.get("title") or "Reading note"
            answer = note.get("answer") or ""
            rendered.append(f"{title}:\n{answer[:1200]}")
        parts.append("Reading results:\n" + "\n\n".join(rendered))

    summary = agent_outputs.get("summary_agent", {}).get("result", {})
    if summary.get("final_report"):
        parts.append("Conversation summary:\n" + str(summary["final_report"])[:1600])

    note = agent_outputs.get("note_agent", {}).get("result", {})
    if note.get("reply"):
        parts.append("Note result:\n" + str(note["reply"])[:800])

    return "\n\n---\n\n".join(parts)


def _fallback_reply(user_message: str, gathered_context: str) -> str:
    if gathered_context:
        return (
            "我已经拿到了一些上下文，但生成综合回答时模型调用失败。"
            "你可以让我继续，我会基于已有检索/阅读结果再整理一版。"
        )
    return (
        "我理解这是一个开放任务，应该先规划再执行。"
        "不过当前 General Agent 的模型调用失败了。你可以补充目标、约束和期望输出，我再继续处理。"
    )
