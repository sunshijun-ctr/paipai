import asyncio
import os
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool
from app.tools.image.ocr_engine import PaddleOCREngine
from app.tools.image.schemas import AnalyzeImageInput, AnalyzeImageOutput, OCRResult, VLMResult
from app.tools.image.vlm_engine import VLMConfig, VLMEngine, default_vlm_config


class AnalyzeImageTool(BaseTool):
    name = "analyze_image_tool"
    description = (
        "Analyze an uploaded image with OCR and a vision-language model. "
        "Returns structured image_context for downstream agents."
    )

    def __init__(self) -> None:
        self.ocr_engine = PaddleOCREngine(lang="ch")

    def preheat_ocr(self) -> None:
        self.ocr_engine.preheat()

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Local image path"},
                "user_question": {"type": "string", "description": "Question about the image"},
                "task_type": {
                    "type": "string",
                    "description": "Image task type",
                    "default": "auto",
                },
                "use_ocr": {"type": "boolean", "default": True},
                "use_vlm": {"type": "boolean", "default": True},
                "vlm_provider": {"type": "string", "description": "Optional VLM provider override"},
                "vlm_model_name": {"type": "string", "description": "Optional VLM model override"},
            },
            "required": ["image_path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        tool_input = AnalyzeImageInput(**kwargs)
        output = await self.run(tool_input)
        return ToolResult(
            success=output.success,
            data=output.model_dump(),
            error=output.error_message,
        )

    async def run(self, tool_input: AnalyzeImageInput) -> AnalyzeImageOutput:
        if not os.path.exists(tool_input.image_path):
            return self._error(tool_input, f"image not found: {tool_input.image_path}")

        ocr_result = OCRResult()
        vlm_result = VLMResult()
        issues: list[str] = []
        tasks: list[tuple[str, asyncio.Task]] = []

        if tool_input.use_ocr:
            tasks.append(("ocr", asyncio.create_task(self._run_ocr(tool_input.image_path))))

        if tool_input.use_vlm:
            tasks.append(("vlm", asyncio.create_task(self._run_vlm(tool_input))))

        for task_name, task in tasks:
            try:
                value = await task
            except Exception as exc:
                issues.append(f"{task_name.upper()} unavailable or failed: {exc}")
                continue
            if task_name == "ocr":
                ocr_result = value
            elif task_name == "vlm":
                vlm_result = value

        if issues:
            vlm_result.potential_issues.extend(issues)

        image_type = self._merge_image_type(
            task_type=tool_input.task_type,
            vlm_image_type=vlm_result.image_type,
            ocr_text=ocr_result.text,
        )
        context_for_agent = self._build_context_for_agent(
            image_type=image_type,
            user_question=tool_input.user_question,
            ocr_result=ocr_result,
            vlm_result=vlm_result,
        )

        return AnalyzeImageOutput(
            success=bool(context_for_agent),
            image_type=image_type,
            user_question=tool_input.user_question,
            ocr_result=ocr_result,
            vlm_result=vlm_result,
            context_for_agent=context_for_agent,
            error_message="; ".join(issues) if issues and not context_for_agent else None,
        )

    async def _run_ocr(self, image_path: str) -> OCRResult:
        return await asyncio.to_thread(self.ocr_engine.extract_text_persistent, image_path)

    async def _run_vlm(self, tool_input: AnalyzeImageInput) -> VLMResult:
        vlm_config = self._vlm_config(tool_input)
        return await VLMEngine(vlm_config).analyze_image(
            image_path=tool_input.image_path,
            user_question=tool_input.user_question,
            ocr_text="",
            task_type=tool_input.task_type,
        )

    def _vlm_config(self, tool_input: AnalyzeImageInput) -> VLMConfig:
        config = default_vlm_config()
        if tool_input.vlm_provider:
            config.provider = tool_input.vlm_provider
        if tool_input.vlm_model_name:
            config.model_name = tool_input.vlm_model_name
        return config

    def _merge_image_type(self, task_type: str, vlm_image_type: str, ocr_text: str) -> str:
        if task_type != "auto":
            return task_type
        if vlm_image_type and vlm_image_type != "unknown":
            return vlm_image_type

        lower_text = ocr_text.lower()
        if any(k in lower_text for k in ["abstract", "introduction", "method", "experiment", "references"]):
            return "paper_screenshot"
        if any(k in lower_text for k in ["traceback", "error", "exception", "failed", "not found"]):
            return "error_screenshot"
        if any(k in lower_text for k in ["accuracy", "precision", "recall", "f1", "map", "auc"]):
            return "chart_screenshot"
        return "daily_screenshot"

    def _build_context_for_agent(
        self,
        image_type: str,
        user_question: str | None,
        ocr_result: OCRResult,
        vlm_result: VLMResult,
    ) -> str:
        key_info_text = "\n".join(f"- {item}" for item in vlm_result.key_information)
        issue_text = "\n".join(f"- {item}" for item in vlm_result.potential_issues)

        if not any([ocr_result.text, vlm_result.visual_summary, key_info_text, issue_text]):
            return ""

        return f"""
你将收到一段由图像理解 Tool 生成的图片上下文。

图片类型：
{image_type}

用户问题：
{user_question or "用户没有提出具体问题，请根据图片内容自然回复。"}

OCR 识别文本：
{ocr_result.text if ocr_result.text else "未识别到有效文字。"}

OCR 平均置信度：
{ocr_result.confidence if ocr_result.confidence is not None else "未知"}

视觉理解摘要：
{vlm_result.visual_summary if vlm_result.visual_summary else "无视觉理解摘要。"}

关键信息：
{key_info_text if key_info_text else "无。"}

潜在不确定性：
{issue_text if issue_text else "无。"}

请基于以上图片上下文回答用户问题。
如果是论文截图，请使用科研助手风格，解释背景、含义、方法或结果。
如果是报错截图，请指出可能原因和排查方向。
如果是表格或图表截图，请解释表格或图表含义。
如果 OCR 或视觉理解结果不充分，请明确说明不确定性。
""".strip()

    def _error(self, tool_input: AnalyzeImageInput, message: str) -> AnalyzeImageOutput:
        return AnalyzeImageOutput(
            success=False,
            image_type="unknown",
            user_question=tool_input.user_question,
            ocr_result=OCRResult(),
            vlm_result=VLMResult(),
            context_for_agent="",
            error_message=message,
        )
