from app.agents.base.agent import BaseAgent
from app.state.task_state import TaskState
from app.workflows.base import AgentWorkflowState, BuildAgentInput, ProgressCallback, build_sequence_graph
from app.workflows.registry import build_registered_workflow, get_workflow_route


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
    """Build and execute the selected LangGraph workflow."""
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
    final_state = await graph.ainvoke(initial)
    return final_state.get("task_state", task_state)


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
