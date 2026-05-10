import logging
import os

from app.agents.base.agent import BaseAgent
from app.config.settings import settings
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMMessage
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = """\
You are a research summary agent. Based on the conversation history of a research session, produce a structured report:
1. Session overview (1-2 sentences on what was explored)
2. Papers discussed (list with titles if mentioned)
3. Key findings (bullet list — the most important insights from Q&A)
4. Open questions / unresolved issues (bullet list)
5. Suggested next actions (bullet list)

Format your response as clear markdown. Be concise but complete.
When your answer includes mathematical formulas: inline formulas use $...$, block formulas use $$...$$. \
Never output bare LaTeX without delimiters.\
"""

_MEMORY_EXTRACT_SYSTEM = """\
You are a memory extraction assistant. Given a research session summary, extract structured memory candidates.
Return ONLY a JSON array — no explanation, no markdown.

Each item must have one of these shapes:
  {"type": "profile",    "data": {"background": "...", "research_directions": [...], "long_term_topics": [...]}}
  {"type": "preference", "data": {"output_style": "...", "communication_style": "...", "task_patterns": [...]}}
  {"type": "conclusion", "content": "one-sentence insight", "topic": "field or paper name"}

Rules:
- Only emit items with non-empty, non-trivial values.
- Omit any field that cannot be inferred from the session.
- "conclusion" items must be specific, reusable insights (not vague platitudes).
- Return [] if nothing is worth remembering.\
"""


class SummaryAgent(BaseAgent):
    name = "summary_agent"
    description = "Generates a structured final report from all agent outputs."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        conversation_history: list[dict] = agent_input.input_data.get("conversation_history", [])
        summary_scope: dict = agent_input.input_data.get("summary_scope", {})
        history_summary: str = agent_input.input_data.get("history_summary", "")

        if conversation_history:
            turns = [
                f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
                for m in conversation_history
            ]
            scope_desc = summary_scope.get("description") or "Summarize the supplied conversation history."
            context_parts = [f"Summary request scope: {scope_desc}"]
            if history_summary and summary_scope.get("mode") == "whole_conversation":
                context_parts.append("Earlier compressed conversation summary:\n\n" + history_summary)
            context_parts.append("Conversation history to summarize:\n\n" + "\n\n".join(turns))
            context_text = "\n\n".join(context_parts)
        else:
            agent_outputs = agent_input.input_data.get("agent_outputs", state.agent_outputs)
            context_text = f"User goal: {agent_input.user_goal}\n\n"
            if summary_scope:
                context_text += f"Summary request scope: {summary_scope.get('description', '')}\n\n"
            reading_notes = agent_outputs.get("reading_agent", {}).get("result", {}).get("reading_notes", [])
            for note in reading_notes:
                context_text += f"Paper: {note['title']}\nQ: {note['question']}\nA: {note['answer']}\n\n"

        state.update_stage("final_summary", self.name)

        # ── Step 1: Generate markdown report ─────────────────────────────────
        try:
            llm_resp = await self.llm.complete(
                messages=[LLMMessage(role="user", content=context_text)],
                system=_SUMMARY_SYSTEM,
            )
            report_text = llm_resp.content
        except Exception as exc:
            logger.warning("SummaryAgent LLM call failed: %s", exc)
            report_text = _mock_report(agent_input.user_goal)

        # ── Step 2: Extract structured memory candidates (separate LLM call) ─
        memory_candidates: list[dict] = []
        try:
            raw = await self.llm.complete_json(
                messages=[LLMMessage(role="user", content=(
                    f"Extract memory candidates from this session summary:\n\n{report_text}"
                ))],
                system=_MEMORY_EXTRACT_SYSTEM,
            )
            if isinstance(raw, list):
                memory_candidates = raw
            elif isinstance(raw, dict):
                # Some providers wrap the list: {"candidates": [...]}
                for v in raw.values():
                    if isinstance(v, list):
                        memory_candidates = v
                        break
            logger.info("SummaryAgent extracted %d memory candidate(s)", len(memory_candidates))
        except Exception as exc:
            logger.debug("Memory candidate extraction failed: %s", exc)

        # ── Persist report to disk ────────────────────────────────────────────
        reports_dir = os.path.join(settings.data_dir, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        report_path = os.path.join(reports_dir, f"{agent_input.task_id}_summary.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)

        state.summary = report_text
        state.update_stage("completed", self.name)

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result={"final_report": report_text},
            artifacts={"report_path": report_path},
            memory_candidates=memory_candidates,
            next_suggestion="end_task_or_continue",
        )


def _mock_report(user_goal: str) -> str:
    return f"""# Research Session Summary

## Session Overview
Completed research task: {user_goal}

## Papers Discussed
- (No papers recorded in this session)

## Key Findings
- Reviewed relevant literature on the topic

## Open Questions
- Further analysis needed

## Suggested Next Actions
- Continue reading related papers
"""
