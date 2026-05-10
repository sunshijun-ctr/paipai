import logging
from typing import Any

from app.agents.base.agent import BaseAgent
from app.agents.writing.service import WritingService
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.schemas.writing import RetrievedChunk, WritingAgentInput, WritingConstraints
from app.services.llm import BaseLLMProvider
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)


class WritingAgent(BaseAgent):
    name = "writing_agent"
    description = "Generates academic writing content from retrieved or uploaded research materials."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.service = WritingService(llm)

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        state.update_stage("paper_writing", self.name)

        payload = _build_writing_payload(agent_input, state)
        result = await self.service.generate(payload)
        status = AgentStatus.SUCCESS
        if not payload.retrieved_chunks and not (payload.user_provided_material or "").strip():
            status = AgentStatus.PARTIAL_SUCCESS

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=status,
            result=result.model_dump(),
            next_suggestion="review_or_save_to_notes",
            memory_candidates=[
                {
                    "type": "writing_output",
                    "content": result.content,
                    "metadata": {
                        "task_type": result.task_type,
                        "title": result.title,
                    },
                }
            ] if result.content else [],
        )


def _build_writing_payload(agent_input: AgentInput, state: TaskState) -> WritingAgentInput:
    data = agent_input.input_data
    constraints = data.get("constraints") or {}
    if isinstance(constraints, WritingConstraints):
        writing_constraints = constraints
    else:
        writing_constraints = WritingConstraints(**{
            k: v for k, v in constraints.items()
            if k in WritingConstraints.model_fields
        })

    return WritingAgentInput(
        user_query=data.get("user_query") or agent_input.user_goal,
        writing_task_type=data.get("writing_task_type") or "literature_review",
        retrieved_chunks=_normalize_chunks(data.get("retrieved_chunks") or []),
        retrieval_summary=data.get("retrieval_summary") or "",
        constraints=writing_constraints,
        user_extra_instruction=data.get("user_extra_instruction") or "",
        user_provided_material=data.get("user_provided_material") or "",
        source_policy=data.get("source_policy") or "",
    )


def _normalize_chunks(chunks: list[dict[str, Any]]) -> list[RetrievedChunk]:
    normalized: list[RetrievedChunk] = []
    for idx, chunk in enumerate(chunks, start=1):
        metadata = chunk.get("metadata") or {}
        source_type = metadata.get("source_type") or metadata.get("rag_type") or "retrieval"
        is_upload = source_type == "upload"
        fallback_id = f"{'upload' if is_upload else 'retrieval'}_chunk_{idx:03d}"
        chunk_id = (
            chunk.get("chunk_id")
            or chunk.get("id")
            or metadata.get("chunk_id")
            or fallback_id
        )
        normalized.append(
            RetrievedChunk(
                chunk_id=str(chunk_id),
                paper_id=chunk.get("paper_id") or metadata.get("paper_id"),
                title=chunk.get("title") or metadata.get("title") or metadata.get("file_name"),
                authors=chunk.get("authors") or metadata.get("authors") or [],
                year=_safe_int(chunk.get("year") or metadata.get("year")),
                section=chunk.get("section") or metadata.get("section"),
                page=_safe_int(chunk.get("page") or metadata.get("page")),
                content=chunk.get("content") or chunk.get("document") or "",
                score=float(chunk.get("score") or chunk.get("distance") or 0.0),
                metadata={**metadata, "source_type": source_type},
            )
        )
    return [c for c in normalized if c.content.strip()]


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
