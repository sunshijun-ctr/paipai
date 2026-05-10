from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.workflows.base import AgentWorkflowState, BuildAgentInput, ProgressCallback, make_agent_node


def build_note_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    builder = StateGraph(AgentWorkflowState)
    builder.add_node("detect_note_action", _detect_note_action_node)
    for node_name in ("create_note", "update_note", "delete_note", "query_note", "embed_note", "organize_note"):
        builder.add_node(
            node_name,
            make_agent_node(
                workflow_name=workflow_name,
                agent_name="note_agent",
                agents=agents,
                build_agent_input=build_agent_input,
                progress=progress,
            ),
        )
    builder.set_entry_point("detect_note_action")
    builder.add_conditional_edges(
        "detect_note_action",
        _note_action_route,
        {
            "create": "create_note",
            "update": "update_note",
            "delete": "delete_note",
            "query": "query_note",
            "embed": "embed_note",
            "organize": "organize_note",
        },
    )
    for node_name in ("create_note", "update_note", "delete_note", "query_note", "embed_note", "organize_note"):
        builder.add_edge(node_name, END)
    return builder.compile()


async def _detect_note_action_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    intent = task_state.agent_outputs.get("intent_agent", {}).get("result", {})
    user_intent = intent.get("user_intent") or intent.get("intent") or "create_note"
    task_state.working_memory["note_action"] = _normalize_note_action(user_intent)
    state["agent_names"] = ["note_agent"]
    state["total_agents"] = 1
    state["done_agents"] = 0
    return state


def _note_action_route(state: AgentWorkflowState) -> str:
    return state["task_state"].working_memory.get("note_action", "create")


def _normalize_note_action(intent: str) -> str:
    mapping = {
        "create_note": "create",
        "create_note_from_chat": "create",
        "create_note_from_summary": "create",
        "create_note_from_reading": "create",
        "update_note": "update",
        "delete_note": "delete",
        "search_note": "query",
        "list_notes": "query",
        "embed_note": "embed",
        "reembed_note": "embed",
        "note": "create",
        "organize_note": "organize",
    }
    return mapping.get(intent, "create")
