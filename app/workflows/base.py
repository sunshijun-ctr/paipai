import logging
from typing import Awaitable, Callable, TypedDict

from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentStatus
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, str, int | None], Awaitable[None]]
BuildAgentInput = Callable[[str, str, TaskState], AgentInput]


class AgentWorkflowState(TypedDict, total=False):
    task_state: TaskState
    user_query: str
    agent_names: list[str]
    total_agents: int
    done_agents: int
    stopped: bool


def build_sequence_graph(
    *,
    workflow_name: str,
    agent_names: list[str],
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    builder = StateGraph(AgentWorkflowState)

    if not agent_names:
        builder.add_node("noop", noop_node)
        builder.set_entry_point("noop")
        builder.add_edge("noop", END)
        return builder.compile()

    for index, agent_name in enumerate(agent_names):
        builder.add_node(
            node_name(index, agent_name),
            make_agent_node(
                workflow_name=workflow_name,
                agent_name=agent_name,
                agents=agents,
                build_agent_input=build_agent_input,
                progress=progress,
            ),
        )

    builder.set_entry_point(node_name(0, agent_names[0]))
    for index, agent_name in enumerate(agent_names):
        current = node_name(index, agent_name)
        if index == len(agent_names) - 1:
            builder.add_edge(current, END)
        else:
            builder.add_conditional_edges(
                current,
                continue_or_end,
                {
                    "continue": node_name(index + 1, agent_names[index + 1]),
                    "end": END,
                },
            )
    return builder.compile()


async def noop_node(state: AgentWorkflowState) -> AgentWorkflowState:
    return state


def make_agent_node(
    *,
    workflow_name: str,
    agent_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    async def node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        done_agents = int(state.get("done_agents", 0))
        total_agents = int(state.get("total_agents", 1))
        agent = agents.get(agent_name)
        if agent is None:
            logger.warning("[%s] Agent '%s' not found, skipping.", task_state.task_id, agent_name)
            state["done_agents"] = done_agents + 1
            return state

        agent_input = build_agent_input(agent_name, state["user_query"], task_state)
        logger.info("[%s] [%s] Running %s...", task_state.task_id, workflow_name, agent_name)
        await progress(agent_name, agent_running_text(agent_name), 35 + int(done_agents / total_agents * 40))
        output = await agent.run(agent_input, task_state)
        task_state.record_agent_output(agent_name, output.model_dump())
        done_agents += 1
        state["done_agents"] = done_agents
        await progress(f"{agent_name}_done", agent_done_text(agent_name), 35 + int(done_agents / total_agents * 40))

        if output.status == AgentStatus.FAILED:
            if (
                agent_name == "retrieval_agent"
                and "writing_agent" in state.get("agent_names", [])
                and has_uploaded_writing_docs(task_state)
            ):
                logger.warning(
                    "[%s] retrieval failed, continuing writing with uploaded materials: %s",
                    task_state.task_id,
                    output.errors,
                )
                return state
            task_state.add_error(f"{agent_name} failed: {output.errors}")
            logger.error("[%s] %s failed, stopping workflow.", task_state.task_id, agent_name)
            state["stopped"] = True
        return state

    return node


def continue_or_end(state: AgentWorkflowState) -> str:
    return "end" if state.get("stopped") else "continue"


def node_name(index: int, agent_name: str) -> str:
    return f"{index:02d}_{agent_name}"


def has_uploaded_writing_docs(task_state: TaskState) -> bool:
    import os

    return any(
        p.get("source") == "upload" and p.get("local_path") and os.path.exists(p.get("local_path", ""))
        for p in task_state.working_memory.get("stored_papers", [])
    )


def agent_short_name(agent: str) -> str:
    return {
        "literature_agent": "literature search",
        "retrieval_agent": "knowledge retrieval",
        "reading_agent": "reading",
        "web_agent": "web search and reading",
        "writing_agent": "writing",
        "summary_agent": "summary",
        "note_agent": "note",
        "general_agent": "general planning",
        "chat_agent": "chat",
        "analyze_image": "image analysis",
    }.get(agent, agent)


def agent_running_text(agent: str) -> str:
    return {
        "literature_agent": "Searching and filtering papers...",
        "retrieval_agent": "Retrieving relevant library passages...",
        "reading_agent": "Reading materials and preparing an answer...",
        "web_agent": "Searching the web, reading pages, and preparing an answer...",
        "writing_agent": "Generating writing content from selected materials...",
        "summary_agent": "Summarizing the current conversation...",
        "note_agent": "Processing note task...",
        "general_agent": "Planning and executing the open task...",
        "chat_agent": "Generating reply...",
        "analyze_image": "Analyzing uploaded image...",
    }.get(agent, f"Running {agent_short_name(agent)}...")


def agent_done_text(agent: str) -> str:
    return {
        "literature_agent": "Paper processing complete.",
        "retrieval_agent": "Knowledge retrieval complete.",
        "reading_agent": "Reading complete.",
        "web_agent": "Web reading complete.",
        "writing_agent": "Writing content generated.",
        "summary_agent": "Summary complete.",
        "note_agent": "Note task complete.",
        "general_agent": "Open task handled.",
        "chat_agent": "Reply generated.",
        "analyze_image": "Image analysis complete.",
    }.get(agent, f"{agent_short_name(agent)} complete.")
