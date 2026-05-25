"""Phase-C smoke: research_agent_workflow's Postgres checkpointer.

Verifies:
  1. AsyncPostgresSaver initialises against DATABASE_URL (tables are
     created idempotently in `checkpoints` etc.).
  2. Running the 3-node graph writes ≥3 checkpoint rows for the task's
     thread_id — one after each node.
  3. Subsequent runs with a new task_id get their own thread, don't
     collide with earlier checkpoints.

Skipped (with [SKIP]) when DATABASE_URL is unset or unreachable — the
graph degrades to in-memory state and that's the documented behavior.

Run from project root:
    python scripts/smoke_research_checkpointer.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

# psycopg's async pool needs SelectorEventLoop on Windows (default is Proactor)
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
    get_research_checkpointer,
    init_research_checkpointer,
)
from app.schemas.agent import AgentInput
from app.services.llm import BaseLLMProvider, LLMResponse
from app.state.task_state import TaskState
from app.workflows.executor import run_agent_workflow


class ScriptedLLM(BaseLLMProvider):
    """Same stub used in smoke_research_agent.py."""
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


def _count_checkpoints_for_thread(thread_id: str) -> int:
    """Direct SQL count from the langgraph-checkpoint `checkpoints` table."""
    import psycopg
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                (thread_id,),
            )
            return cur.fetchone()[0]


async def main():
    if not (settings.database_url or "").strip():
        print("[SKIP] DATABASE_URL not set — checkpointer is disabled by design")
        return

    print("=" * 60)
    print("Phase C smoke: AsyncPostgresSaver persistence")
    print("=" * 60)

    saver = await init_research_checkpointer()
    if saver is None:
        print("[FAIL] init_research_checkpointer returned None — DB unreachable")
        return
    print("  [OK] checkpointer initialised")

    # Patch the orchestrator's research_agent so the graph runs without
    # touching real LLM endpoints.
    llm = ScriptedLLM(
        {"thinking": "noop", "steps": [{"id": 1, "tool": "note_list", "args": {}}]},
        "stub answer",
    )
    agents = dict(_O._agents)
    agents["research_agent"] = ResearchAgent(llm)

    async def _prog(*a, **kw): pass

    def _bi(name, query, task_state):
        return AgentInput(
            task_id=task_state.task_id, session_id=task_state.session_id,
            agent_name=name, user_goal=query, current_stage="dispatch", input_data={},
        )

    # Run 1
    tid1 = f"ckpt-test-{uuid.uuid4().hex[:8]}"
    state1 = TaskState(user_goal="run 1", session_id="ckpt-s1")
    state1.task_id = tid1
    await run_agent_workflow(
        workflow_name="research_agent_workflow",
        task_state=state1,
        user_query="research notes",
        agent_names=["research_agent"],
        agents=agents,
        build_agent_input=_bi,
        progress=_prog,
    )
    n1 = _count_checkpoints_for_thread(tid1)
    print(f"  thread {tid1}: {n1} checkpoint row(s)")
    assert n1 >= 3, f"expected ≥3 checkpoints (one per node), got {n1}"
    print("  [OK] >=3 checkpoints persisted for run 1")

    # Run 2 — separate thread, no row collision
    tid2 = f"ckpt-test-{uuid.uuid4().hex[:8]}"
    state2 = TaskState(user_goal="run 2", session_id="ckpt-s2")
    state2.task_id = tid2
    await run_agent_workflow(
        workflow_name="research_agent_workflow",
        task_state=state2,
        user_query="research notes 2",
        agent_names=["research_agent"],
        agents=agents,
        build_agent_input=_bi,
        progress=_prog,
    )
    n2 = _count_checkpoints_for_thread(tid2)
    print(f"  thread {tid2}: {n2} checkpoint row(s)")
    assert n2 >= 3
    assert n1 == _count_checkpoints_for_thread(tid1), "run 2 contaminated run 1's thread"
    print("  [OK] separate threads stay isolated")

    # Verify graph still completed normally (final answer recorded)
    out = state2.agent_outputs.get("research_agent", {}).get("result", {})
    assert out.get("reply") == "stub answer", f"unexpected reply: {out.get('reply')!r}"
    print("  [OK] graph still produces the synthesised answer alongside checkpoints")

    await close_research_checkpointer()
    print("  [OK] checkpointer closed cleanly")
    print()
    print("Phase C smoke: PASSED")


if __name__ == "__main__":
    asyncio.run(main())
