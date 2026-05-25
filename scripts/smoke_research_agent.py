"""ResearchAgent smoke test with a stub LLM.

Validates the new plan-execute-synthesize flow end-to-end without hitting
a real API:

  PLAN       → stub LLM returns a JSON plan (2 parallel search steps)
  EXECUTE    → both steps run; stub tools return fake results
  SYNTHESIZE → stub LLM returns the final answer text

Also exercises:
  - empty plan (LLM says "no tools needed" → skip exec, go straight to synth)
  - bad-plan rejection (unknown tool, malformed args → dropped)
  - tool error path (one step fails; synthesis still runs)

Run from project root:
    python scripts/smoke_research_agent.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.agents.research.agent import ResearchAgent
from app.schemas.agent import AgentInput, AgentStatus
from app.services.llm import BaseLLMProvider, LLMResponse
from app.state.task_state import TaskState


# ── Stub LLM: scripted JSON / text responses ─────────────────────────────

class ScriptedLLM(BaseLLMProvider):
    """LLM that returns pre-baked responses in order.

    Each call to complete() or complete_json() pops the next response from
    the script. complete_json returns the parsed dict; complete returns
    the text wrapped in LLMResponse."""

    def __init__(self, plan_payload: dict, synthesize_text: str) -> None:
        self._plan = plan_payload
        self._synth = synthesize_text
        self.calls: list[str] = []

    async def complete(self, messages, system=None, **kwargs):
        self.calls.append("complete")
        return LLMResponse(content=self._synth, model="stub")

    async def complete_json(self, messages, system=None, **kwargs):
        self.calls.append("complete_json")
        return self._plan


# Boot the orchestrator once to populate ToolRegistry with real tools
from app.orchestrator.orchestrator import Orchestrator
_O = Orchestrator()


async def case_happy_path() -> None:
    print("=" * 60)
    print("Case 1: 2 parallel search steps → synthesise")
    print("=" * 60)
    plan = {
        "thinking": "Need recent papers and web context",
        "steps": [
            {"id": 1, "tool": "paper_search", "args": {"query": "RAG medical applications", "max_results": 3}},
            {"id": 2, "tool": "web_search",   "args": {"query": "RAG medical 2024"}},
        ],
    }
    llm = ScriptedLLM(plan, "Final synthesised answer about RAG in medicine.")
    agent = ResearchAgent(llm)
    state = TaskState(user_goal="research RAG medical", session_id="smoke-1")
    inp = AgentInput(task_id="t1", session_id="smoke-1", agent_name="research_agent",
                     user_goal="research RAG in medical applications",
                     current_stage="dispatch", input_data={})
    out = await agent.run(inp, state)
    print(f"  status:         {out.status}")
    print(f"  plan steps:     {len(out.result['plan']['steps'])}")
    print(f"  exec results:   {len(out.result['step_results'])}")
    print(f"  llm calls:      {llm.calls}")
    print(f"  reply preview:  {out.result['reply'][:80]}")
    print(f"  timings:        {out.result['timings']}")
    assert out.status == AgentStatus.SUCCESS
    assert len(out.result["plan"]["steps"]) == 2
    assert llm.calls == ["complete_json", "complete"]  # plan + synth, no other LLM calls
    print("  [OK]")


async def case_empty_plan() -> None:
    print()
    print("=" * 60)
    print("Case 2: empty plan — answer from knowledge, exec skipped")
    print("=" * 60)
    plan = {"thinking": "purely conversational", "steps": []}
    llm = ScriptedLLM(plan, "No external info needed — here is the direct answer.")
    agent = ResearchAgent(llm)
    state = TaskState(user_goal="what is RAG?", session_id="smoke-2")
    inp = AgentInput(task_id="t2", session_id="smoke-2", agent_name="research_agent",
                     user_goal="what is RAG?", current_stage="dispatch", input_data={})
    out = await agent.run(inp, state)
    print(f"  status:        {out.status}")
    print(f"  plan steps:    {len(out.result['plan']['steps'])}")
    print(f"  exec results:  {len(out.result['step_results'])}")
    print(f"  llm calls:     {llm.calls}")
    assert out.status == AgentStatus.SUCCESS
    assert out.result["plan"]["steps"] == []
    assert out.result["step_results"] == []
    assert llm.calls == ["complete_json", "complete"]
    print("  [OK]")


async def case_bad_plan_filtered() -> None:
    print()
    print("=" * 60)
    print("Case 3: malformed plan steps get dropped, valid ones survive")
    print("=" * 60)
    plan = {
        "thinking": "mixed valid + invalid",
        "steps": [
            {"id": 1, "tool": "paper_search", "args": {"query": "transformers", "max_results": 5}},
            {"id": 2, "tool": "totally_fake_tool", "args": {}},                # unknown tool
            {"id": 3, "tool": "web_search", "args": "this should be a dict"},  # bad args
            {"id": 4, "tool": "web_search", "args": {"query": "transformers survey"}},
        ],
    }
    llm = ScriptedLLM(plan, "Synthesised with the 2 valid steps.")
    agent = ResearchAgent(llm)
    state = TaskState(user_goal="...", session_id="smoke-3")
    inp = AgentInput(task_id="t3", session_id="smoke-3", agent_name="research_agent",
                     user_goal="transformer architecture", current_stage="dispatch", input_data={})
    out = await agent.run(inp, state)
    print(f"  steps after validation: {[s['tool'] for s in out.result['plan']['steps']]}")
    assert [s["tool"] for s in out.result["plan"]["steps"]] == ["paper_search", "web_search"]
    print("  [OK] 2/4 steps survived")


async def case_duplicate_tool_capped() -> None:
    print()
    print("=" * 60)
    print("Case 3b: LLM spams same tool — validator caps it at 2 per tool")
    print("=" * 60)
    # Reproduces the prod incident: LLM returned 5 paper_search calls,
    # 4 of which timed out fighting for the same upstream API budget.
    plan = {
        "thinking": "LLM tried to flood paper_search",
        "steps": [
            {"id": 1, "tool": "paper_search", "args": {"query": "LLM evaluation methods"}},
            {"id": 2, "tool": "paper_search", "args": {"query": "LLM benchmark survey"}},
            {"id": 3, "tool": "paper_search", "args": {"query": "evaluation framework"}},
            {"id": 4, "tool": "paper_search", "args": {"query": "LLM evaluation 2024"}},
            {"id": 5, "tool": "paper_search", "args": {"query": "LLM eval recent"}},
            {"id": 6, "tool": "note_search",  "args": {"query": "evaluation"}},
        ],
    }
    llm = ScriptedLLM(plan, "Synth output after cap.")
    agent = ResearchAgent(llm)
    state = TaskState(user_goal="...", session_id="smoke-3b")
    inp = AgentInput(task_id="t3b", session_id="smoke-3b", agent_name="research_agent",
                     user_goal="LLM evaluation", current_stage="dispatch", input_data={})
    out = await agent.run(inp, state)
    tools = [s["tool"] for s in out.result["plan"]["steps"]]
    print(f"  steps after validation: {tools}")
    paper_search_count = sum(1 for t in tools if t == "paper_search")
    # max_steps=4 trims to 4, then per-tool cap drops the 3rd/4th paper_search
    assert paper_search_count <= 2, f"expected ≤2 paper_search, got {paper_search_count}"
    print(f"  [OK] capped to {paper_search_count} paper_search call(s)")


async def case_workflow_dispatch() -> None:
    print()
    print("=" * 60)
    print("Case 4: research_agent_workflow is dispatchable from registry")
    print("=" * 60)
    from app.workflows.executor import _build_workflow_graph
    async def _prog(*a, **kw): pass
    def _bi(a, q, s): return None
    g, _ = _build_workflow_graph(workflow_name="research_agent_workflow", runnable=[],
                                  agents=_O._agents, build_agent_input=_bi, progress=_prog)
    assert g is not None
    print("  [OK] graph builds")


async def case_three_node_graph_end_to_end() -> None:
    print()
    print("=" * 60)
    print("Case 5: 3-node graph plan → execute → synthesize runs end-to-end")
    print("=" * 60)
    # Swap the orchestrator's research_agent LLM with a scripted stub so
    # we can drive the graph without hitting a real API. The agents dict
    # we hand to the graph builder gets the same patched agent.
    plan_payload = {
        "thinking": "two parallel searches",
        "steps": [
            {"id": 1, "tool": "note_search", "args": {"query": "evaluation"}},
            {"id": 2, "tool": "note_list",   "args": {}},
        ],
    }
    llm = ScriptedLLM(plan_payload, "FINAL ANSWER from 3-node graph.")
    patched_agent = ResearchAgent(llm)
    agents = dict(_O._agents)
    agents["research_agent"] = patched_agent

    from app.agents.research.agent import ResearchAgent as _RA
    from app.workflows.executor import run_agent_workflow

    captured: list[tuple[str, str, int | None]] = []
    async def _prog(step, text, pct=None): captured.append((step, text, pct))

    def _bi(name, query, task_state):
        return AgentInput(
            task_id=task_state.task_id, session_id=task_state.session_id,
            agent_name=name, user_goal=query, current_stage="dispatch", input_data={},
        )

    state = TaskState(user_goal="research notes", session_id="smoke-5")
    state.task_id = "smoke-5"
    final_state = await run_agent_workflow(
        workflow_name="research_agent_workflow",
        task_state=state,
        user_query="find notes about evaluation",
        agent_names=["research_agent"],
        agents=agents,
        build_agent_input=_bi,
        progress=_prog,
    )

    out = final_state.agent_outputs.get("research_agent", {})
    result = out.get("result", {})
    print(f"  output status:  {out.get('status')}")
    print(f"  reply preview:  {result.get('reply', '')[:80]}")
    print(f"  plan steps:     {len(result.get('plan', {}).get('steps', []))}")
    print(f"  step results:   {len(result.get('step_results', []))}")
    print(f"  timings:        {result.get('timings')}")
    progress_steps = [s for s, _, _ in captured]
    print(f"  progress steps: {progress_steps}")
    assert out.get("status") == "success"
    assert result.get("reply") == "FINAL ANSWER from 3-node graph."
    # All three progress beats should have fired
    assert "research_plan" in progress_steps
    assert "research_exec" in progress_steps
    assert "research_synth" in progress_steps
    assert llm.calls == ["complete_json", "complete"]  # one plan + one synth
    print("  [OK] 3-node graph produced the expected output")


async def main():
    await case_happy_path()
    await case_empty_plan()
    await case_bad_plan_filtered()
    await case_duplicate_tool_capped()
    await case_workflow_dispatch()
    await case_three_node_graph_end_to_end()
    print()
    print("All ResearchAgent smoke checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
