from typing import Literal, Optional

from pydantic import BaseModel, Field


ImageTaskType = Literal[
    "auto",
    "paper_screenshot",
    "paper_figure",
    "formula_screenshot",
    "table_screenshot",
    "error_screenshot",
    "ui_screenshot",
    "daily_screenshot",
    "chart_screenshot",
]


class AnalyzeImageInput(BaseModel):
    image_path: str = Field(..., description="Local image path")
    user_question: Optional[str] = Field(default=None, description="User question about the image")
    task_type: ImageTaskType = Field(default="auto", description="Image task type")
    use_ocr: bool = Field(default=True, description="Enable OCR")
    use_vlm: bool = Field(default=True, description="Enable vision-language model")
    vlm_provider: Optional[str] = Field(default=None, description="Override VLM provider")
    vlm_model_name: Optional[str] = Field(default=None, description="Override VLM model")


class OCRResult(BaseModel):
    text: str = Field(default="", description="Recognized text")
    confidence: Optional[float] = Field(default=None, description="Average OCR confidence")


class VLMResult(BaseModel):
    visual_summary: str = Field(default="", description="Visual summary")
    image_type: str = Field(default="unknown", description="Detected image type")
    key_information: list[str] = Field(default_factory=list, description="Key information")
    potential_issues: list[str] = Field(default_factory=list, description="Uncertainties or risks")


class AnalyzeImageOutput(BaseModel):
    success: bool
    image_type: str
    user_question: Optional[str]
    ocr_result: OCRResult
    vlm_result: VLMResult
    context_for_agent: str
    error_message: Optional[str] = None
