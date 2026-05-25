import logging
import re

from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.state.task_state import TaskState
from app.tools.base import ToolRegistry
from app.workflows.base import (
    AgentWorkflowState,
    BuildAgentInput,
    ProgressCallback,
    continue_or_end,
    make_agent_node,
)

logger = logging.getLogger(__name__)

_IMAGE_PATH_RE = re.compile(r"(?:^|\n)IMAGE_PATH=(.+?)(?:\n|$)")


def build_image_understanding_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    builder = StateGraph(AgentWorkflowState)
    builder.add_node("analyze_image", _analyze_image_node(progress))
    builder.add_node(
        "general_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="general_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )
    builder.set_entry_point("analyze_image")
    builder.add_conditional_edges(
        "analyze_image",
        continue_or_end,
        {
            "continue": "general_agent",
            "end": END,
        },
    )
    builder.add_edge("general_agent", END)
    return builder.compile()


def _analyze_image_node(progress: ProgressCallback):
    async def node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        user_query = state["user_query"]
        image_path = extract_image_path(user_query)
        clean_query = strip_image_path_marker(user_query)

        state["agent_names"] = ["analyze_image", "general_agent"]
        state["total_agents"] = 2
        state["done_agents"] = 0

        if not image_path:
            task_state.add_error("No uploaded image was provided for image understanding.")
            state["stopped"] = True
            return state

        await progress("analyze_image", "Analyzing uploaded image...", 35)
        try:
            result = await ToolRegistry.get("analyze_image_tool").execute(
                image_path=image_path,
                user_question=clean_query,
                task_type="auto",
                use_ocr=True,
                use_vlm=True,
            )
        except Exception as exc:
            logger.exception("[%s] Image analysis failed", task_state.task_id)
            task_state.add_error(f"Image analysis failed: {exc}")
            state["stopped"] = True
            return state

        task_state.tool_results["image_analysis"] = result.data if result.success else {"error": result.error}
        task_state.record_agent_output(
            "image_analysis_tool",
            {
                "agent_name": "image_analysis_tool",
                "status": "success" if result.success else "failed",
                "result": result.data if result.success else {},
                "errors": [] if result.success else [result.error or "image analysis failed"],
            },
        )

        if not result.success:
            task_state.add_error(result.error or "image analysis failed")
            state["stopped"] = True
            return state

        image_context = str(result.data.get("context_for_agent") or "").strip()
        if not image_context:
            image_context = "图片分析工具未提取到有效上下文，请根据用户问题说明当前不确定性。"

        task_state.working_memory["image_context"] = image_context
        task_state.working_memory["image_path"] = image_path
        state["user_query"] = (
            f"{clean_query}\n\n"
            "[image_context]\n"
            f"{image_context}"
        )
        state["done_agents"] = 1
        await progress("analyze_image_done", "Image analysis complete.", 55)
        return state

    return node


def extract_image_path(user_query: str) -> str:
    match = _IMAGE_PATH_RE.search(user_query or "")
    return match.group(1).strip() if match else ""


def strip_image_path_marker(user_query: str) -> str:
    return _IMAGE_PATH_RE.sub("\n", user_query or "").strip()


def has_image_path_marker(user_query: str) -> bool:
    return bool(extract_image_path(user_query))
