"""ResearchAgent — plan-and-execute architecture.

Replaces the legacy ReAct-style "one tool call per LLM round" loop with:

    user_query ─┐
                ▼
    [1] PLAN       LLM(complete_json) → {"steps": [{tool, args}, ...]}
                ▼
    [2] EXECUTE    asyncio.gather(*[tool.execute(**args) for step in steps])
                ▼
    [3] SYNTHESIZE LLM(complete) → final Markdown answer

The three phases are exposed as public methods (`plan`, `execute`,
`synthesize`) so the langgraph workflow can wrap each in its own node —
that's what lets us insert a human-in-the-loop interrupt between PLAN
and EXECUTE in a later phase. `run` keeps the all-in-one entry point
for direct invocation (tests, scripts, single-call use).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from app.agents.base.agent import BaseAgent
from app.agents.research.prompt import render_plan_prompt, render_synthesize_prompt
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMMessage
from app.state.task_state import TaskState
from app.tools.base import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


# Tools the planner is allowed to schedule. Restricted to independent
# data-fetching tools — anything that depends on a prior step's output
# (paper_filter / paper_download / note_create / note_update) doesn't
# work in a single-pass plan-execute model and would only succeed via a
# replan loop (Phase C). The synthesis phase does ranking/comparison on
# its own from the collected results.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "paper_search",       # external paper discovery (Semantic Scholar / arXiv)
    "library_search",     # the user's permanent indexed paper library
    "web_search",         # the open web
    "web_fetch",          # fetch one URL's content
    "note_search",        # the user's free-form personal notes (NOT papers)
    "note_list",          # list those notes
)

DEFAULT_MAX_STEPS = 4
MAX_CALLS_PER_TOOL = 2          # cap on duplicates of the same tool in one plan
DEFAULT_TOOL_TIMEOUT_SECONDS = 45.0
TOOL_RESULT_TRUNCATE_CHARS = 4000
SYNTHESIZE_CONTEXT_BUDGET_CHARS = 24000  # total budget for step-results in the synth prompt

ProgressFn = Callable[[str, str, Optional[int]], Awaitable[None]]


def tool_label(name: str) -> str:
    """Chinese-friendly UI label for a tool name. Exported so the
    workflow's progress callback can show the same labels as the agent's
    own internal progress emission."""
    return {
        "paper_search": "搜索论文（外部）",
        "library_search": "搜索文献库",
        "web_search": "搜索网页",
        "web_fetch": "抓取网页",
        "note_search": "查找便签",
        "note_list": "列出便签",
    }.get(name, name)


class ResearchAgent(BaseAgent):
    name = "research_agent"
    description = (
        "Open-ended research assistant. Plans a parallel data-fetching batch, "
        "runs all tools concurrently, then synthesises a final answer."
    )

    def __init__(
        self,
        llm: BaseLLMProvider,
        *,
        allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS,
        max_steps: int = DEFAULT_MAX_STEPS,
        tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        self.llm = llm
        self.allowed_tools = allowed_tools
        self.max_steps = max_steps
        self.tool_timeout_seconds = tool_timeout_seconds

    # ── Phase 1: PLAN ────────────────────────────────────────────────────

    def _tool_summary(self) -> str:
        """Short text listing of tools for the plan prompt. Skips
        tools that aren't currently registered."""
        lines: list[str] = []
        for tool_name in self.allowed_tools:
            try:
                tool = ToolRegistry.get(tool_name)
            except KeyError:
                logger.warning("ResearchAgent: tool '%s' not registered, skipping", tool_name)
                continue
            schema_str = json.dumps(tool.input_schema, ensure_ascii=False)
            lines.append(f"- `{tool_name}` — {tool.description}\n  args schema: {schema_str}")
        return "\n".join(lines)

    def _history_messages(self, state: TaskState) -> list[LLMMessage]:
        """Last few user/assistant turns from the conversation, for context."""
        out: list[LLMMessage] = []
        history = state.working_memory.get("conversation_history") or []
        for turn in history[-6:]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if not content or role not in {"user", "assistant"}:
                continue
            out.append(LLMMessage(role=role, content=content[:2000]))
        return out

    async def plan(
        self,
        agent_input: AgentInput,
        state: TaskState,
    ) -> dict[str, Any]:
        """One LLM call to produce a parallel-execution plan as JSON."""
        tool_summary = self._tool_summary()
        if not tool_summary:
            return {"thinking": "no tools registered", "steps": []}

        system_prompt = render_plan_prompt(
            tool_summary=tool_summary,
            max_steps=self.max_steps,
        )
        messages = self._history_messages(state)
        messages.append(LLMMessage(role="user", content=agent_input.user_goal))

        raw: dict[str, Any]
        try:
            raw = await self.llm.complete_json(
                messages=messages,
                system=system_prompt,
                task_name="research_agent.plan",
                session_id=agent_input.session_id,
            )
        except Exception as exc:
            logger.warning("ResearchAgent.plan: LLM call failed: %s — falling back to empty plan", exc)
            return {"thinking": f"plan failed: {exc}", "steps": []}

        steps = self._validate_plan(raw)
        return {"thinking": (raw.get("thinking") or "").strip(), "steps": steps}

    def _validate_plan(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """Reject any step that names an unknown tool, has malformed args,
        or exceeds the per-tool duplicate cap. Returns the (possibly empty)
        cleaned step list."""
        steps_in = raw.get("steps") if isinstance(raw, dict) else None
        if not isinstance(steps_in, list):
            return []
        clean: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        per_tool_count: dict[str, int] = {}
        for raw_step in steps_in[: self.max_steps]:
            if not isinstance(raw_step, dict):
                continue
            tool_name = (raw_step.get("tool") or "").strip()
            if tool_name not in self.allowed_tools:
                logger.warning("ResearchAgent.plan: dropping step with disallowed tool '%s'", tool_name)
                continue
            try:
                ToolRegistry.get(tool_name)
            except KeyError:
                logger.warning("ResearchAgent.plan: dropping step '%s' — tool not registered", tool_name)
                continue
            # Cap calls to the same tool — parallel duplicates burn time on
            # rate-limited upstream APIs (e.g. Semantic Scholar) for marginal
            # extra info. The LLM is prompted to avoid this; this is the
            # enforced backstop.
            if per_tool_count.get(tool_name, 0) >= MAX_CALLS_PER_TOOL:
                logger.warning(
                    "ResearchAgent.plan: dropping extra '%s' call (cap=%d)",
                    tool_name, MAX_CALLS_PER_TOOL,
                )
                continue
            args = raw_step.get("args") or {}
            if not isinstance(args, dict):
                logger.warning("ResearchAgent.plan: dropping step with non-dict args: %r", raw_step)
                continue
            try:
                step_id = int(raw_step.get("id") or (len(clean) + 1))
            except (TypeError, ValueError):
                step_id = len(clean) + 1
            if step_id in seen_ids:
                step_id = max(seen_ids) + 1
            seen_ids.add(step_id)
            per_tool_count[tool_name] = per_tool_count.get(tool_name, 0) + 1
            clean.append({"id": step_id, "tool": tool_name, "args": args})
        return clean

    # ── Phase 2: EXECUTE ─────────────────────────────────────────────────

    async def _exec_step(self, step: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call with a hard timeout. Always returns a dict
        with shape {"step": ..., "tool": ..., "ok": bool, "data"|"error": ...}."""
        tool_name = step["tool"]
        args = step["args"]
        try:
            tool: BaseTool = ToolRegistry.get(tool_name)
        except KeyError:
            return {**step, "ok": False, "error": f"tool '{tool_name}' not available"}
        try:
            result = await asyncio.wait_for(
                tool.execute(**args),
                timeout=self.tool_timeout_seconds,
            )
            payload = result.model_dump()
            return {
                "step": step["id"],
                "tool": tool_name,
                "args": args,
                "ok": bool(payload.get("success", False)),
                "data": payload.get("data"),
                "error": payload.get("error"),
            }
        except asyncio.TimeoutError:
            logger.warning(
                "ResearchAgent.exec: tool '%s' timed out after %.1fs",
                tool_name, self.tool_timeout_seconds,
            )
            return {
                "step": step["id"],
                "tool": tool_name,
                "args": args,
                "ok": False,
                "error": f"timeout after {self.tool_timeout_seconds:.0f}s",
            }
        except Exception as exc:
            logger.warning("ResearchAgent.exec: tool '%s' raised: %s", tool_name, exc)
            return {
                "step": step["id"],
                "tool": tool_name,
                "args": args,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def execute(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Run every plan step concurrently. Progress emission is the
        caller's job (workflow node) — this method is pure compute."""
        steps = plan["steps"]
        if not steps:
            return []
        t0 = time.time()
        results = await asyncio.gather(*(self._exec_step(s) for s in steps))
        logger.info(
            "[research_agent] executed %d step(s) in parallel: %s [%.1fs]",
            len(steps), ", ".join(s["tool"] for s in steps), time.time() - t0,
        )
        return results

    # ── Phase 3: SYNTHESIZE ──────────────────────────────────────────────

    @staticmethod
    def _format_step_results(results: list[dict[str, Any]]) -> str:
        """Render step results into a budget-bounded text block for the
        synthesis prompt. Each step's data is JSON-encoded then truncated
        per-step, with a hard total budget so a single huge result can't
        starve the rest."""
        if not results:
            return "(no tool calls were made — answer from your own knowledge if appropriate)"
        per_step_budget = max(1000, SYNTHESIZE_CONTEXT_BUDGET_CHARS // len(results))
        chunks: list[str] = []
        for r in results:
            header = f"## Step {r['step']} — {r['tool']}({json.dumps(r['args'], ensure_ascii=False)[:300]})"
            if not r["ok"]:
                chunks.append(f"{header}\nERROR: {r.get('error') or 'unknown failure'}")
                continue
            try:
                body = json.dumps(r.get("data"), ensure_ascii=False, indent=None)
            except Exception:
                body = str(r.get("data"))
            if len(body) > per_step_budget:
                body = body[:per_step_budget] + " …(truncated)"
            chunks.append(f"{header}\n{body}")
        return "\n\n".join(chunks)

    async def synthesize(
        self,
        agent_input: AgentInput,
        state: TaskState,
        plan: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> str:
        messages = self._history_messages(state)
        plan_brief = plan.get("thinking") or ""
        user_block = f"User request:\n{agent_input.user_goal}"
        if plan_brief:
            user_block += f"\n\nPlanner thinking: {plan_brief}"
        user_block += "\n\nStep results:\n" + self._format_step_results(results)
        messages.append(LLMMessage(role="user", content=user_block))

        response = await self.llm.complete(
            messages=messages,
            system=render_synthesize_prompt(),
            task_name="research_agent.synthesize",
            session_id=agent_input.session_id,
        )
        return response.content.strip()

    @staticmethod
    def build_output(
        agent_input: AgentInput,
        plan: dict[str, Any],
        results: list[dict[str, Any]],
        final_text: str,
        timings: dict[str, float],
    ) -> AgentOutput:
        """Package plan + results + answer into the standard AgentOutput
        shape that downstream reply extractors expect."""
        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name="research_agent",
            status=AgentStatus.SUCCESS,
            result={
                "reply": final_text or "(empty answer)",
                "answer": final_text or "(empty answer)",
                "plan": plan,
                "step_results": [
                    {"step": r["step"], "tool": r["tool"], "ok": r["ok"], "error": r.get("error")}
                    for r in results
                ],
                "timings": {k: round(v, 2) for k, v in timings.items()},
            },
        )

    # ── Orchestration (single-call entry point) ──────────────────────────

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        """All-in-one entry point. The workflow graph splits this into
        three nodes; this method keeps the original contract working for
        direct invocation (tests, scripts, fallback paths)."""
        progress: ProgressFn = agent_input.context.get("__progress__") or _noop_progress
        run_t0 = time.time()

        # Phase 1: plan
        await progress("research_plan", "研究中 · 正在规划步骤…", 35)
        plan_t0 = time.time()
        plan = await self.plan(agent_input, state)
        plan_secs = time.time() - plan_t0
        logger.info(
            "[research_agent] plan ready: %d step(s) [%.1fs] thinking=%s",
            len(plan["steps"]), plan_secs, (plan.get("thinking") or "")[:120],
        )

        # Phase 2: execute (no-op if plan is empty)
        if plan["steps"]:
            labels = " + ".join(tool_label(s["tool"]) for s in plan["steps"])
            await progress("research_exec", f"研究中 · 并发执行 {len(plan['steps'])} 步：{labels}…", 55)
        exec_t0 = time.time()
        results = await self.execute(plan)
        exec_secs = time.time() - exec_t0

        # Phase 3: synthesize
        await progress("research_synth", "研究中 · 正在汇总答案…", 85)
        synth_t0 = time.time()
        try:
            final_text = await self.synthesize(agent_input, state, plan, results)
        except Exception as exc:
            logger.warning("ResearchAgent.synthesize: LLM call failed: %s", exc)
            return self._error_output(
                agent_input,
                f"synthesis failed after {len(results)} step(s): {type(exc).__name__}: {exc}",
            )
        synth_secs = time.time() - synth_t0

        timings = {
            "plan_secs": plan_secs,
            "exec_secs": exec_secs,
            "synth_secs": synth_secs,
            "total_secs": time.time() - run_t0,
        }
        logger.info(
            "[research_agent] done [plan %.1fs · exec %.1fs · synth %.1fs · total %.1fs] "
            "steps=%d ok=%d",
            plan_secs, exec_secs, synth_secs, timings["total_secs"],
            len(results), sum(1 for r in results if r["ok"]),
        )
        return self.build_output(agent_input, plan, results, final_text, timings)


async def _noop_progress(_step: str, _text: str, _pct: Optional[int] = None) -> None:
    return None
