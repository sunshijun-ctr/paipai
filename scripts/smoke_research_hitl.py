"""Phase-D smoke: ResearchAgent HITL approve / modify / cancel / timeout.

For each case the graph runs with `research_hitl_enabled=True` and a
plan that hits the threshold (default min_steps=3). The smoke harness
plays the role of the resume endpoint, calling `signal_resume(task_id,
decision)` from a parallel task.

We assert:
  approve   → original plan ran, synthesis produced reply
  modify    → modified plan ran (different step count)
  cancel    → no exec, synthesis still produced a (degraded) reply
  timeout   → no signal_resume call within ~2s; auto-approve kicks in
              and the original plan ran

Run from project root:
    python scripts/smoke_research_hitl.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

# psycopg async pool needs SelectorEventLoop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.agents.research.agent import ResearchAgent
from app.config.settings import settings
from app.orchestrator.research_checkpointer import (
    close_research_checkpointer,
    init_research_checkpointer,
)
from app.orchestrator.research_hitl import has_pending, signal_resume
from app.schemas.agent import AgentInput
from app.services.llm import BaseLLMProvider, LLMResponse
from app.state.task_state import TaskState
from app.workflows.executor import run_agent_workflow


class ScriptedLLM(BaseLLMProvider):
    def __init__(self, plan, synth):
        self._plan = plan
        self._synth = synth
        self.calls = []

    async def complete(self, messages, system=None, **kwargs):
        self.calls.append("complete")
        return LLMResponse(content=self._synth, model="stub")

    async def complete_json(self, messages, system=None, **kwargs):
        self.calls.append("complete_json")
        return self._plan


from app.orchestrator.orchestrator import Orchestrator
_O = Orchestrator()


def _captured_events():
    captured = []
    async def _prog(payload):
        captured.append(payload)
    return captured, _prog


def _build_input(name, query, task_state):
    return AgentInput(
        task_id=task_state.task_id, session_id=task_state.session_id,
        agent_name=name, user_goal=query, current_stage="dispatch", input_data={},
    )


def _three_step_plan():
    return {
        "thinking": "needs three independent fetches",
        "steps": [
            {"id": 1, "tool": "note_list",   "args": {}},
            {"id": 2, "tool": "note_search", "args": {"query": "a"}},
            {"id": 3, "tool": "note_search", "args": {"query": "b"}},
        ],
    }


async def _run_one(plan_payload, synth_text, resumer):
    """Run one HITL flow. *resumer* is a coro that takes the task_id and
    decides when/how to signal_resume; pass None to test the timeout
    path."""
    llm = ScriptedLLM(plan_payload, synth_text)
    agents = dict(_O._agents)
    agents["research_agent"] = ResearchAgent(llm)
    # Use the orchestrator's progress_callback shape (dict-receiving)
    captured = []
    async def _prog_dict(payload):
        captured.append(payload)
    # Wrap as the (step,text,pct,*,data) signature the workflow expects
    async def _prog(step, text, pct=None, *, data=None):
        msg = {"step": step, "text": text, "pct": pct}
        if data is not None: msg["data"] = data
        await _prog_dict(msg)

    state = TaskState(user_goal="hitl probe", session_id=f"hitl-{uuid.uuid4().hex[:4]}")
    state.task_id = f"hitl-task-{uuid.uuid4().hex[:8]}"

    bg = None
    if resumer is not None:
        bg = asyncio.create_task(resumer(state.task_id))

    out_state = await run_agent_workflow(
        workflow_name="research_agent_workflow",
        task_state=state,
        user_query="hitl probe",
        agent_names=["research_agent"],
        agents=agents,
        build_agent_input=_build_input,
        progress=_prog,
    )
    if bg is not None:
        await bg

    return out_state, captured, llm


async def case_approve():
    print("=" * 60)
    print("Case A: approve → original plan executes")
    print("=" * 60)
    async def _resume(tid):
        # Wait until the checkpoint is registered (executor opens it AFTER
        # the graph's first ainvoke pauses). Poll briefly.
        for _ in range(50):
            if has_pending(tid):
                break
            await asyncio.sleep(0.05)
        signal_resume(tid, {"action": "approve"})

    out_state, captured, _ = await _run_one(_three_step_plan(), "APPROVE SYNTH", _resume)
    result = out_state.agent_outputs.get("research_agent", {}).get("result", {})
    types_emitted = [
        p.get("data", {}).get("type") for p in captured if isinstance(p.get("data"), dict)
    ]
    print(f"  reply:        {result.get('reply')}")
    print(f"  plan steps:   {len(result.get('plan', {}).get('steps', []))}")
    print(f"  emitted evt:  {types_emitted}")
    assert "research_plan_checkpoint" in types_emitted, "plan card event should have fired"
    assert result.get("reply") == "APPROVE SYNTH"
    assert len(result.get("plan", {}).get("steps", [])) == 3
    print("  [OK] plan card fired, original plan ran, synth returned")


async def case_modify():
    print()
    print("=" * 60)
    print("Case B: modify → user-edited plan executes (different step count)")
    print("=" * 60)
    async def _resume(tid):
        for _ in range(50):
            if has_pending(tid): break
            await asyncio.sleep(0.05)
        # User drops two steps, keeps one
        signal_resume(tid, {
            "action": "modify",
            "modified_plan": {
                "thinking": "user trimmed",
                "steps": [{"id": 1, "tool": "note_list", "args": {}}],
            },
        })

    out_state, _, _ = await _run_one(_three_step_plan(), "MODIFY SYNTH", _resume)
    result = out_state.agent_outputs.get("research_agent", {}).get("result", {})
    print(f"  plan steps after modify: {len(result.get('plan', {}).get('steps', []))}")
    assert len(result.get("plan", {}).get("steps", [])) == 1
    assert result.get("reply") == "MODIFY SYNTH"
    print("  [OK] modified plan was respected")


async def case_cancel():
    print()
    print("=" * 60)
    print("Case C: cancel → no execution, synth still runs (degraded)")
    print("=" * 60)
    async def _resume(tid):
        for _ in range(50):
            if has_pending(tid): break
            await asyncio.sleep(0.05)
        signal_resume(tid, {"action": "cancel"})

    out_state, _, _ = await _run_one(_three_step_plan(), "CANCEL SYNTH", _resume)
    result = out_state.agent_outputs.get("research_agent", {}).get("result", {})
    print(f"  plan steps after cancel: {len(result.get('plan', {}).get('steps', []))}")
    print(f"  step results:            {len(result.get('step_results', []))}")
    assert result.get("plan", {}).get("steps") == []
    assert result.get("step_results") == []
    assert result.get("reply") == "CANCEL SYNTH"
    print("  [OK] cancel cleared the plan; synth ran from empty results")


async def case_timeout():
    print()
    print("=" * 60)
    print("Case D: no resume signal → auto-approve after timeout")
    print("=" * 60)
    # Bypass the 60s production default by patching the setting just for
    # this case. 2s is plenty.
    original = settings.research_hitl_timeout_secs
    settings.research_hitl_timeout_secs = 2
    try:
        out_state, _, _ = await _run_one(_three_step_plan(), "TIMEOUT SYNTH", None)
    finally:
        settings.research_hitl_timeout_secs = original

    result = out_state.agent_outputs.get("research_agent", {}).get("result", {})
    print(f"  plan steps:   {len(result.get('plan', {}).get('steps', []))}")
    print(f"  reply:        {result.get('reply')}")
    assert len(result.get("plan", {}).get("steps", [])) == 3, "auto-approve should keep original plan"
    assert result.get("reply") == "TIMEOUT SYNTH"
    print("  [OK] auto-approve ran the original plan")


async def case_skip_below_threshold():
    print()
    print("=" * 60)
    print("Case E: plans below threshold skip the checkpoint entirely")
    print("=" * 60)
    plan_small = {"thinking": "small", "steps": [{"id": 1, "tool": "note_list", "args": {}}]}
    # No resumer at all — if a checkpoint were opened we'd hit the
    # default 60s timeout. Run with a short timeout just in case.
    original = settings.research_hitl_timeout_secs
    settings.research_hitl_timeout_secs = 2
    try:
        out_state, captured, _ = await _run_one(plan_small, "SMALL SYNTH", None)
    finally:
        settings.research_hitl_timeout_secs = original
    types_emitted = [
        p.get("data", {}).get("type") for p in captured if isinstance(p.get("data"), dict)
    ]
    result = out_state.agent_outputs.get("research_agent", {}).get("result", {})
    print(f"  emitted evt:  {types_emitted}")
    print(f"  reply:        {result.get('reply')}")
    assert "research_plan_checkpoint" not in types_emitted
    assert result.get("reply") == "SMALL SYNTH"
    print("  [OK] 1-step plan never paused — fast path preserved")


async def main():
    if not (settings.database_url or "").strip():
        print("[SKIP] DATABASE_URL not set — HITL needs the checkpointer")
        return

    saver = await init_research_checkpointer()
    if saver is None:
        print("[FAIL] checkpointer init failed — cannot test interrupt/resume")
        return

    # Enable HITL for the duration of this test
    original = settings.research_hitl_enabled
    settings.research_hitl_enabled = True
    try:
        await case_approve()
        await case_modify()
        await case_cancel()
        await case_timeout()
        await case_skip_below_threshold()
    finally:
        settings.research_hitl_enabled = original
        await close_research_checkpointer()

    print()
    print("All HITL smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
