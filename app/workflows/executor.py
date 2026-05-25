import logging

from langgraph.types import Command

from app.agents.base.agent import BaseAgent
from app.config.settings import settings
from app.orchestrator.research_hitl import (
    cancel_pending,
    open_checkpoint,
    wait_for_resume,
)
from app.state.task_state import TaskState
from app.workflows.base import AgentWorkflowState, BuildAgentInput, ProgressCallback, build_sequence_graph
from app.workflows.registry import build_registered_workflow, get_workflow_route

logger = logging.getLogger(__name__)


# Safety net: a single research query may resume at most this many
# times before the executor gives up. Prevents pathological loops if a
# node is buggy and re-interrupts immediately after resume.
_MAX_RESUMES_PER_TASK = 3


async def run_agent_workflow(
    *,
    workflow_name: str,
    task_state: TaskState,
    user_query: str,
    agent_names: list[str],
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
) -> TaskState:
    """Build and execute the selected LangGraph workflow.

    If the graph hits an ``interrupt()`` (currently only ResearchAgent's
    plan-approval checkpoint), this function:

      1. Registers a pending checkpoint keyed by ``task_state.task_id``.
      2. Waits up to ``research_hitl_timeout_secs`` for the resume API
         endpoint to call :func:`signal_resume` with the user's decision.
      3. On timeout the default decision is ``{"action": "approve"}`` —
         the plan runs as-is, so unattended sessions still complete.
      4. Calls ``graph.ainvoke(Command(resume=decision), config)`` to
         continue execution from the paused state.
    """
    runnable = [name for name in agent_names if name != "intent_agent"]
    graph, runnable = _build_workflow_graph(
        workflow_name=workflow_name,
        runnable=runnable,
        agents=agents,
        build_agent_input=build_agent_input,
        progress=progress,
    )

    initial: AgentWorkflowState = {
        "task_state": task_state,
        "user_query": user_query,
        "agent_names": runnable,
        "total_agents": max(len(runnable), 1),
        "done_agents": 0,
        "stopped": False,
    }
    config = {"configurable": {"thread_id": task_state.task_id}}
    final_state = await graph.ainvoke(initial, config=config)

    # Resume loop — handles any interrupt() the graph hit. Only
    # research_agent_workflow uses interrupts right now; other workflows
    # have no interrupt nodes so `_graph_is_paused` is always False and
    # this loop body never executes.
    resumes = 0
    while await _graph_is_paused(graph, config):
        if resumes >= _MAX_RESUMES_PER_TASK:
            logger.warning(
                "[%s] hit resume cap (%d) — aborting workflow",
                task_state.task_id, _MAX_RESUMES_PER_TASK,
            )
            cancel_pending(task_state.task_id)
            break

        timeout = float(settings.research_hitl_timeout_secs)
        owner = str(task_state.working_memory.get("owner_user_id") or "")
        open_checkpoint(task_state.task_id, owner_user_id=owner)
        decision = await wait_for_resume(task_state.task_id, timeout)
        try:
            final_state = await graph.ainvoke(Command(resume=decision), config=config)
        except Exception as exc:
            logger.exception(
                "[%s] resume ainvoke failed: %s", task_state.task_id, exc,
            )
            break
        resumes += 1

    return final_state.get("task_state", task_state)


async def _graph_is_paused(graph, config: dict) -> bool:
    """Detect whether the compiled graph is sitting at an interrupt.

    LangGraph stores pending interrupts in the snapshot's tasks. A
    paused graph has at least one task with a non-empty ``interrupts``
    tuple; a completed graph has no pending tasks at all.
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:
        # Graph without a checkpointer can't report state — treat as
        # completed (no resume needed).
        logger.debug("aget_state failed (%s) — assuming graph completed", exc)
        return False
    for task in (snapshot.tasks or ()):
        if getattr(task, "interrupts", ()):
            return True
    return False


def _build_workflow_graph(
    *,
    workflow_name: str,
    runnable: list[str],
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    route = get_workflow_route(workflow_name)
    if route is not None:
        graph = build_registered_workflow(
            workflow_name=workflow_name,
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        )
        return (
            graph,
            runnable or list(route.default_agents),
        )

    return (
        build_sequence_graph(
            workflow_name=workflow_name,
            agent_names=runnable,
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
        runnable,
    )
