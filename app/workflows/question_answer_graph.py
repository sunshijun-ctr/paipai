import logging

from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.state.task_state import TaskState
from app.workflows.base import (
    AgentWorkflowState,
    BuildAgentInput,
    ProgressCallback,
    continue_or_end,
    has_uploaded_writing_docs,
    make_agent_node,
)
from app.orchestrator.router import looks_like_web_read_query

logger = logging.getLogger(__name__)


def build_question_answer_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    builder = StateGraph(AgentWorkflowState)
    builder.add_node("detect_source", _detect_qa_source_node)
    builder.add_node(
        "retrieval_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="retrieval_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )
    builder.add_node(
        "reading_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="reading_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )
    builder.set_entry_point("detect_source")
    builder.add_conditional_edges(
        "detect_source",
        _qa_source_route,
        {
            "uploaded_file": "reading_agent",
            "library": "retrieval_agent",
            "web": "reading_agent",
        },
    )
    builder.add_conditional_edges(
        "retrieval_agent",
        continue_or_end,
        {
            "continue": "reading_agent",
            "end": END,
        },
    )
    builder.add_edge("reading_agent", END)
    return builder.compile()


async def _detect_qa_source_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    source = detect_question_answer_source(state["user_query"], task_state)
    task_state.working_memory["qa_source"] = source
    if source == "library":
        task_state.working_memory["library_qa_mode"] = True
        state["agent_names"] = ["retrieval_agent", "reading_agent"]
        state["total_agents"] = 2
    else:
        task_state.working_memory.pop("library_qa_mode", None)
        state["agent_names"] = ["reading_agent"]
        state["total_agents"] = 1
    state["done_agents"] = 0
    logger.info("[%s] Question answer source: %s", task_state.task_id, source)
    return state


def _qa_source_route(state: AgentWorkflowState) -> str:
    return state["task_state"].working_memory.get("qa_source", "library")


def detect_question_answer_source(user_query: str, task_state: TaskState) -> str:
    query = (user_query or "").lower()
    has_uploads = has_uploaded_writing_docs(task_state) or bool(task_state.working_memory.get("stored_papers"))
    upload_markers = (
        "\u4e0a\u4f20",
        "\u8fd9\u4e2a\u6587\u4ef6",
        "\u8fd9\u4e2a pdf",
        "\u8fd9\u4e2apdf",
        "\u6587\u4ef6\u91cc",
        "uploaded",
        "this file",
        "this pdf",
    )
    library_markers = (
        "\u77e5\u8bc6\u5e93",
        "\u6587\u732e\u5e93",
        "\u8d44\u6599\u5e93",
        "\u6211\u7684\u6587\u732e",
        "knowledge base",
        "my library",
        "literature library",
    )

    if looks_like_web_read_query(user_query):
        task_state.working_memory["web_read_mode"] = True
        task_state.working_memory.pop("library_qa_mode", None)
        return "web"
    if any(marker in query for marker in library_markers):
        task_state.working_memory.pop("web_read_mode", None)
        return "library"
    if any(marker in query for marker in upload_markers):
        task_state.working_memory.pop("web_read_mode", None)
        return "uploaded_file" if has_uploads else "library"
    task_state.working_memory.pop("web_read_mode", None)
    return "uploaded_file" if has_uploads else "library"
