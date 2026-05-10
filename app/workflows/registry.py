from dataclasses import dataclass
from typing import Callable

from app.agents.base.agent import BaseAgent
from app.workflows.base import BuildAgentInput, ProgressCallback
from app.workflows.chat_graph import build_chat_graph
from app.workflows.conversation_summary_graph import build_conversation_summary_graph
from app.workflows.image_understanding_graph import build_image_understanding_graph
from app.workflows.library_ingest_graph import build_library_ingest_graph
from app.workflows.note_graph import build_note_graph
from app.workflows.paper_search_graph import build_paper_search_graph
from app.workflows.question_answer_graph import build_question_answer_graph
from app.workflows.web_search_graph import build_web_search_graph
from app.workflows.writing_graphs import (
    build_academic_writing_graph,
    build_kb_writing_graph,
    build_uploaded_file_writing_graph,
)


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
        default_agents=("retrieval_agent", "reading_agent"),
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
        default_agents=("analyze_image", "chat_agent"),
        graph_builder=build_image_understanding_graph,
    ),
    "library_ingest_workflow": WorkflowRoute(
        name="library_ingest_workflow",
        description="Validate and ingest files into the literature library.",
        default_agents=("library_tool",),
        graph_builder=build_library_ingest_graph,
    ),
    "academic_writing_workflow": WorkflowRoute(
        name="academic_writing_workflow",
        description="Generate, polish, supplement, paraphrase, or imitate academic writing from user input, uploads, and/or library retrieval.",
        default_agents=("writing_agent",),
        graph_builder=build_academic_writing_graph,
    ),
    "kb_writing_workflow": WorkflowRoute(
        name="kb_writing_workflow",
        description="Retrieve from the knowledge base and generate academic writing.",
        default_agents=("retrieval_agent", "reading_agent", "writing_agent"),
        graph_builder=build_kb_writing_graph,
    ),
    "uploaded_file_writing_workflow": WorkflowRoute(
        name="uploaded_file_writing_workflow",
        description="Parse uploaded files and generate academic writing.",
        default_agents=("writing_agent",),
        graph_builder=build_uploaded_file_writing_graph,
    ),
    "note_workflow": WorkflowRoute(
        name="note_workflow",
        description="Create, update, delete, query, organize, or embed notes.",
        default_agents=("note_agent",),
        graph_builder=build_note_graph,
    ),
    "chat_workflow": WorkflowRoute(
        name="chat_workflow",
        description="General chat or lightweight explanation.",
        default_agents=("chat_agent",),
        graph_builder=build_chat_graph,
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
