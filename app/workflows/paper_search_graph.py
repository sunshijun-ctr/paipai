from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.workflows.base import (
    AgentWorkflowState,
    BuildAgentInput,
    ProgressCallback,
    continue_or_end,
    make_agent_node,
)


def build_paper_search_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    builder = StateGraph(AgentWorkflowState)
    builder.add_node("prepare_search", _prepare_search_node)
    builder.add_node(
        "literature_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="literature_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )
    builder.add_node("present_results", _present_results_node)
    builder.set_entry_point("prepare_search")
    builder.add_edge("prepare_search", "literature_agent")
    builder.add_conditional_edges(
        "literature_agent",
        continue_or_end,
        {
            "continue": "present_results",
            "end": END,
        },
    )
    builder.add_edge("present_results", END)
    return builder.compile()


async def _prepare_search_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    state["agent_names"] = ["literature_agent"]
    state["total_agents"] = 1
    state["done_agents"] = 0
    task_state.working_memory.setdefault("paper_search_query", state.get("user_query", ""))
    return state


async def _present_results_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    output = task_state.agent_outputs.get("literature_agent", {})
    result = output.get("result", {})
    selected = result.get("selected_papers", [])

    if task_state.working_memory.get("search_only") and selected:
        task_state.last_search_results = selected
        task_state.pending_action = {
            "type": "download_choice",
            "workflow": "paper_search_workflow",
            "options": selected,
            "message": "可以回复：下载第1篇、下载前3篇，或不下载。",
        }
    return state
