from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.workflows.base import (
    AgentWorkflowState,
    BuildAgentInput,
    ProgressCallback,
    continue_or_end,
    make_agent_node,
)


def build_conversation_summary_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    builder = StateGraph(AgentWorkflowState)
    builder.add_node("load_conversation", _load_conversation_node)
    builder.add_node(
        "summary_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="summary_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )
    builder.add_node("present_summary", _present_summary_node)
    builder.set_entry_point("load_conversation")
    builder.add_edge("load_conversation", "summary_agent")
    builder.add_conditional_edges(
        "summary_agent",
        continue_or_end,
        {
            "continue": "present_summary",
            "end": END,
        },
    )
    builder.add_edge("present_summary", END)
    return builder.compile()


async def _load_conversation_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    state["agent_names"] = ["summary_agent"]
    state["total_agents"] = 1
    state["done_agents"] = 0
    task_state.working_memory.setdefault("conversation_history", [])
    return state


async def _present_summary_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    summary_text = (
        task_state.agent_outputs.get("summary_agent", {})
        .get("result", {})
        .get("final_report", "")
    )
    if summary_text:
        task_state.last_summary = summary_text
        task_state.pending_action = {
            "type": "save_note_choice",
            "workflow": "conversation_summary_workflow",
            "summary_text": summary_text,
            "message": "是否保存到笔记？可以回复：保存，或不保存。",
        }
    return state
