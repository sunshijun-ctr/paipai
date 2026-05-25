"""4-node langgraph for ResearchAgent:

    plan_node  →  approve_node  →  execute_node  →  synthesize_node  →  END

Each node calls the corresponding method on the agent:

    plan_node       agent.plan(...)        → state["plan"]
    approve_node    interrupt(...) [opt]   → may overwrite state["plan"]
    execute_node    agent.execute(plan)    → state["step_results"]
    synthesize_node agent.synthesize(...)  → state["final_text"]
                                             + records AgentOutput on task_state

The approve node is split from the plan node on purpose: langgraph
re-executes a node from its start when ``interrupt()`` resumes, and we
don't want the (expensive) plan LLM call to happen twice. ``approve``
is cheap to re-run.

When ``settings.research_hitl_enabled`` is False (default) or the plan
has fewer than ``research_hitl_min_steps`` steps, ``approve`` is a
straight pass-through and the graph behaves exactly like the old
3-node Phase-B version.
"""
from __future__ import annotations

import logging
import time

from langchain_core.runnables.config import var_child_runnable_config
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.agents.base.agent import BaseAgent
from app.agents.research.agent import ResearchAgent, tool_label
from app.config.settings import settings
from app.orchestrator.research_checkpointer import get_research_checkpointer
from app.workflows.base import (
    AgentWorkflowState,
    BuildAgentInput,
    ProgressCallback,
    noop_node,
)

logger = logging.getLogger(__name__)


def build_research_agent_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    agent = agents.get("research_agent")
    if not isinstance(agent, ResearchAgent):
        builder = StateGraph(AgentWorkflowState)
        builder.add_node("noop", noop_node)
        builder.set_entry_point("noop")
        builder.add_edge("noop", END)
        return builder.compile()

    builder = StateGraph(AgentWorkflowState)
    builder.add_node("plan", _make_plan_node(agent, workflow_name, build_agent_input, progress))
    builder.add_node("approve", _make_approve_node(agent, workflow_name, progress))
    builder.add_node("execute", _make_execute_node(agent, workflow_name, progress))
    builder.add_node("synthesize", _make_synthesize_node(agent, workflow_name, build_agent_input, progress))
    builder.set_entry_point("plan")
    builder.add_edge("plan", "approve")
    builder.add_edge("approve", "execute")
    builder.add_edge("execute", "synthesize")
    builder.add_edge("synthesize", END)

    # Postgres checkpointer (Phase C) — also required for interrupt()
    # to work (Phase D). Without a checkpointer interrupt has no place
    # to persist its pause state, and the graph compiles as best-effort.
    checkpointer = get_research_checkpointer()
    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


# ── Nodes ─────────────────────────────────────────────────────────────────


def _make_plan_node(
    agent: ResearchAgent,
    workflow_name: str,
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    async def plan_node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        await progress("research_plan", "研究中 · 正在规划步骤…", 35)
        agent_input = build_agent_input("research_agent", state["user_query"], task_state)
        agent_input.context.setdefault("__progress__", progress)
        agent_input.context.setdefault("__workflow_name__", workflow_name)

        t0 = time.time()
        plan = await agent.plan(agent_input, task_state)
        elapsed = time.time() - t0
        logger.info(
            "[%s] [%s] plan ready: %d step(s) [%.1fs] thinking=%s",
            task_state.task_id, workflow_name, len(plan["steps"]), elapsed,
            (plan.get("thinking") or "")[:120],
        )

        timings = dict(state.get("timings") or {})
        timings["plan_secs"] = elapsed
        timings["_run_t0"] = timings.get("_run_t0") or (time.time() - elapsed)
        return {**state, "plan": plan, "timings": timings}

    return plan_node


def _make_approve_node(
    agent: ResearchAgent,
    workflow_name: str,
    progress: ProgressCallback,
):
    # The `config` parameter is auto-passed by langgraph based on the
    # node's signature. We need it because on Python < 3.11, langgraph's
    # `var_child_runnable_config` contextvar does NOT propagate into
    # async tasks — `interrupt()` reads from that contextvar internally,
    # so we re-set it manually here for the duration of the call.
    async def approve_node(state: AgentWorkflowState, config) -> AgentWorkflowState:
        plan = state.get("plan") or {"steps": []}
        steps = plan.get("steps") or []
        task_state = state["task_state"]

        # Gate: HITL must be enabled AND the plan must hit the threshold.
        # Simple plans skip the checkpoint to keep their fast path fast.
        if not settings.research_hitl_enabled or len(steps) < settings.research_hitl_min_steps:
            return state

        # Push the plan to the frontend through the progress channel.
        # The WS handler relays the `data` field as a `research_plan_checkpoint`
        # event the chat UI can render as a plan card.
        timeout_secs = int(settings.research_hitl_timeout_secs)
        await progress(
            "research_plan_checkpoint",
            f"研究计划已就绪（{len(steps)} 步），等待你的确认…",
            40,
            data={
                "type": "research_plan_checkpoint",
                "task_id": task_state.task_id,
                "plan": plan,
                "timeout_secs": timeout_secs,
            },
        )

        # Pause the graph here. The decision dict is whatever the
        # executor passes back via Command(resume=...). Default on
        # timeout is {"action": "approve"}.
        token = var_child_runnable_config.set(config)
        try:
            decision = interrupt({
                "type": "approve_plan",
                "plan": plan,
                "timeout_secs": timeout_secs,
            }) or {}
        finally:
            var_child_runnable_config.reset(token)
        action = (decision.get("action") or "approve").lower()
        logger.info(
            "[%s] [%s] HITL resume: action=%s",
            task_state.task_id, workflow_name, action,
        )

        if action == "cancel":
            # Empty plan → execute is a no-op → synthesize answers from
            # knowledge alone (or refuses politely).
            return {**state, "plan": {"thinking": "user cancelled the plan", "steps": []}}

        if action == "modify":
            modified = decision.get("modified_plan") or {}
            validated = agent._validate_plan(modified)
            if validated:
                return {**state, "plan": {
                    "thinking": (modified.get("thinking") or "user-modified plan"),
                    "steps": validated,
                }}
            logger.warning(
                "[%s] modified_plan was empty after validation — falling back to original",
                task_state.task_id,
            )

        return state

    return approve_node


def _make_execute_node(
    agent: ResearchAgent,
    workflow_name: str,
    progress: ProgressCallback,
):
    async def execute_node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        plan = state.get("plan") or {"steps": []}
        steps = plan.get("steps") or []

        if steps:
            labels = " + ".join(tool_label(s["tool"]) for s in steps)
            await progress(
                "research_exec",
                f"研究中 · 并发执行 {len(steps)} 步：{labels}…",
                55,
            )

        t0 = time.time()
        results = await agent.execute(plan)
        elapsed = time.time() - t0

        if results:
            ok = sum(1 for r in results if r.get("ok"))
            logger.info(
                "[%s] [%s] exec: %d/%d ok [%.1fs]",
                task_state.task_id, workflow_name, ok, len(results), elapsed,
            )

        timings = dict(state.get("timings") or {})
        timings["exec_secs"] = elapsed
        return {**state, "step_results": results, "timings": timings}

    return execute_node


def _make_synthesize_node(
    agent: ResearchAgent,
    workflow_name: str,
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    async def synthesize_node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        plan = state.get("plan") or {"steps": []}
        results = state.get("step_results") or []

        await progress("research_synth", "研究中 · 正在汇总答案…", 85)
        agent_input = build_agent_input("research_agent", state["user_query"], task_state)
        agent_input.context.setdefault("__progress__", progress)
        agent_input.context.setdefault("__workflow_name__", workflow_name)

        t0 = time.time()
        try:
            final_text = await agent.synthesize(agent_input, task_state, plan, results)
            synth_secs = time.time() - t0
            timings = dict(state.get("timings") or {})
            timings["synth_secs"] = synth_secs
            run_t0 = timings.pop("_run_t0", None)
            if run_t0 is not None:
                timings["total_secs"] = time.time() - run_t0
            else:
                timings["total_secs"] = (
                    timings.get("plan_secs", 0.0)
                    + timings.get("exec_secs", 0.0)
                    + synth_secs
                )

            output = agent.build_output(agent_input, plan, results, final_text, timings)
            task_state.record_agent_output("research_agent", output.model_dump())
            logger.info(
                "[%s] [%s] done [plan %.1fs · exec %.1fs · synth %.1fs · total %.1fs] "
                "steps=%d ok=%d",
                task_state.task_id, workflow_name,
                timings.get("plan_secs", 0.0),
                timings.get("exec_secs", 0.0),
                synth_secs,
                timings.get("total_secs", 0.0),
                len(results), sum(1 for r in results if r.get("ok")),
            )
            await progress("research_agent_done", "Research complete.", 90)
            return {**state, "final_text": final_text, "timings": timings}
        except Exception as exc:
            logger.warning(
                "[%s] [%s] synthesis failed: %s",
                task_state.task_id, workflow_name, exc,
            )
            error_out = agent._error_output(
                agent_input,
                f"synthesis failed after {len(results)} step(s): {type(exc).__name__}: {exc}",
            )
            task_state.record_agent_output("research_agent", error_out.model_dump())
            task_state.add_error(f"research_agent failed: {error_out.errors}")
            return {**state, "stopped": True}

    return synthesize_node
