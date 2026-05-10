from app.agents.base.agent import BaseAgent
from app.workflows.base import BuildAgentInput, ProgressCallback, build_sequence_graph


def build_general_agent_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    return build_sequence_graph(
        workflow_name=workflow_name,
        agent_names=["general_agent"],
        agents=agents,
        build_agent_input=build_agent_input,
        progress=progress,
    )
