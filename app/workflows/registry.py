from dataclasses import dataclass
from typing import Callable

from app.agents.base.agent import BaseAgent
from app.workflows.base import BuildAgentInput, ProgressCallback
from app.workflows.conversation_summary_graph import build_conversation_summary_graph
from app.workflows.general_graph import build_general_agent_graph
from app.workflows.image_understanding_graph import build_image_understanding_graph
from app.workflows.paper_search_graph import build_paper_search_graph
from app.workflows.question_answer_graph import build_question_answer_graph
from app.workflows.research_agent_graph import build_research_agent_graph
from app.workflows.web_search_graph import build_web_search_graph
from app.workflows.writing_graphs import build_academic_writing_graph


GraphBuilder = Callable[..., object]


@dataclass(frozen=True)
class WorkflowRoute:
    name: str
    description: str
    default_agents: tuple[str, ...]
    graph_builder: GraphBuilder
    requires_pending_action_support: bool = False


WORKFLOW_REGISTRY: dict[str, WorkflowRoute] = {
    "paper_search_workflow": WorkflowRoute(
        name="paper_search_workflow",
        description="Search papers and optionally continue to user-confirmed downloads.",
        default_agents=("literature_agent",),
        graph_builder=build_paper_search_graph,
        requires_pending_action_support=True,
    ),
    "conversation_summary_workflow": WorkflowRoute(
        name="conversation_summary_workflow",
        description="Summarize the current conversation and optionally save to notes.",
        default_agents=("summary_agent",),
        graph_builder=build_conversation_summary_graph,
        requires_pending_action_support=True,
    ),
    "question_answer_workflow": WorkflowRoute(
        name="question_answer_workflow",
        description="Answer questions using uploaded files or library retrieval.",
        default_agents=("rag_agent",),
        graph_builder=build_question_answer_graph,
    ),
    "web_search_workflow": WorkflowRoute(
        name="web_search_workflow",
        description="Search the web, read selected pages, and synthesize an answer.",
        default_agents=("web_agent",),
        graph_builder=build_web_search_graph,
    ),
    "image_understanding_workflow": WorkflowRoute(
        name="image_understanding_workflow",
        description="Analyze an uploaded image, then answer the user's question in chat.",
        default_agents=("analyze_image", "general_agent"),
        graph_builder=build_image_understanding_graph,
    ),
    # NOTE: the former library_ingest_workflow / note_workflow were demoted to
    # direct action handlers (no branching reasoning to justify a graph) and
    # renamed to library_ingest_action / note_action. They live in
    # app/orchestrator/actions.py and are dispatched by the orchestrator, not
    # compiled here. See ACTION_NAMES in app/schemas/workflow.py.
    "academic_writing_workflow": WorkflowRoute(
        name="academic_writing_workflow",
        description=(
            "Generate, polish, supplement, paraphrase, or imitate academic "
            "writing. Internally detects the source mode (user input / "
            "uploaded files / library retrieval / both) — replaces the old "
            "kb_writing_workflow and uploaded_file_writing_workflow."
        ),
        default_agents=("writing_agent",),
        graph_builder=build_academic_writing_graph,
    ),
    "general_agent_workflow": WorkflowRoute(
        name="general_agent_workflow",
        description=(
            "Open-ended tasks, conversation, comparison, planning — anything "
            "not covered by a strict workflow. Also replaces the old "
            "chat_workflow."
        ),
        default_agents=("general_agent",),
        graph_builder=build_general_agent_graph,
    ),
    "research_agent_workflow": WorkflowRoute(
        name="research_agent_workflow",
        description=(
            "LLM tool-calling agent for multi-step research. Composes "
            "paper_search / web_search / note_* / etc. on its own without "
            "going through a rigid pre-defined node sequence."
        ),
        default_agents=("research_agent",),
        graph_builder=build_research_agent_graph,
    ),
}


def get_workflow_route(workflow_name: str) -> WorkflowRoute | None:
    return WORKFLOW_REGISTRY.get(workflow_name)


def build_registered_workflow(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    route = get_workflow_route(workflow_name)
    if route is None:
        return None
    return route.graph_builder(
        workflow_name=route.name,
        agents=agents,
        build_agent_input=build_agent_input,
        progress=progress,
    )
