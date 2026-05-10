from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


WritingTaskType = Literal[
    "abstract",
    "introduction",
    "related_work",
    "background",
    "method_description",
    "experiment_analysis",
    "conclusion",
    "academic_rewrite",
    "expand_text",
    "summarize_to_paragraph",
    "literature_review",
]


class RetrievedChunk(BaseModel):
    chunk_id: str
    paper_id: Optional[str] = None
    title: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    section: Optional[str] = None
    page: Optional[int] = None
    content: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class WritingConstraints(BaseModel):
    language: Literal["zh", "en"] = "zh"
    style: Literal["academic", "concise", "formal", "review"] = "academic"
    length: Literal["short", "medium", "long"] = "medium"
    citation_required: bool = True
    allow_model_reasoning: bool = True
    avoid_fabrication: bool = True


class WritingAgentInput(BaseModel):
    user_query: str
    writing_task_type: str = "literature_review"
    retrieved_chunks: list[RetrievedChunk] = Field(default_factory=list)
    retrieval_summary: Optional[str] = None
    constraints: WritingConstraints = Field(default_factory=WritingConstraints)
    user_extra_instruction: Optional[str] = None
    user_provided_material: Optional[str] = None
    source_policy: Optional[str] = None


class CitationRef(BaseModel):
    ref_id: str
    chunk_id: str
    title: Optional[str] = None
    year: Optional[int] = None
    page: Optional[int] = None


class WritingAgentOutput(BaseModel):
    task_type: str
    title: Optional[str] = None
    content: str
    citations: list[CitationRef] = Field(default_factory=list)
    material_usage_summary: str = ""
    limitations: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
