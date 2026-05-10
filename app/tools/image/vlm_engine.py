import base64
import json
import mimetypes
from dataclasses import dataclass
from typing import Optional

from app.config.settings import settings
from app.tools.image.schemas import VLMResult


@dataclass
class VLMConfig:
    provider: str = "openai_compatible"
    model_name: str = "qwen-vl"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 2048


def default_vlm_config() -> VLMConfig:
    provider = settings.vlm_provider or "openai_compatible"
    return VLMConfig(
        provider=provider,
        model_name=settings.vlm_model_name or _default_model_for_provider(provider),
        base_url=settings.vlm_base_url or _default_base_url_for_provider(provider),
        api_key=settings.vlm_api_key or _default_api_key_for_provider(provider),
        temperature=settings.vlm_temperature,
        max_tokens=settings.vlm_max_tokens,
    )


def _default_model_for_provider(provider: str) -> str:
    if provider == "qwen_vl":
        return settings.qwen_vl_model or "qwen-vl-max"
    if provider == "gpt_4o":
        return settings.openai_model or "gpt-4o"
    if provider == "gemini_vision":
        return settings.gemini_model
    return settings.vlm_model_name or "qwen-vl"


def _default_api_key_for_provider(provider: str) -> Optional[str]:
    if provider == "qwen_vl":
        return settings.qwen_api_key
    if provider == "gpt_4o":
        return settings.openai_api_key
    if provider == "gemini_vision":
        return settings.gemini_api_key
    return settings.vlm_api_key


def _default_base_url_for_provider(provider: str) -> Optional[str]:
    if provider == "qwen_vl":
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if provider == "gemini_vision":
        return settings.gemini_base_url
    return None


class OpenAICompatibleVisionClient:
    def __init__(self, config: VLMConfig) -> None:
        from openai import AsyncOpenAI

        self.config = config
        self.client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "EMPTY",
        )

    async def analyze(self, image_path: str, prompt: str) -> str:
        with open(image_path, "rb") as fh:
            image_base64 = base64.b64encode(fh.read()).decode("utf-8")
        media_type = mimetypes.guess_type(image_path)[0] or "image/png"

        response = await self.client.chat.completions.create(
            model=self.config.model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_base64}",
                            },
                        },
                    ],
                }
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )
        return response.choices[0].message.content or ""


class VLMEngine:
    def __init__(self, config: Optional[VLMConfig] = None) -> None:
        self.config = config or default_vlm_config()
        self.client = self._build_client(self.config)

    def _build_client(self, config: VLMConfig):
        if config.provider in {"openai_compatible", "qwen_vl", "gpt_4o", "gemini_vision"}:
            return OpenAICompatibleVisionClient(config)
        raise ValueError(f"Unsupported VLM provider: {config.provider}")

    async def analyze_image(
        self,
        image_path: str,
        user_question: Optional[str] = None,
        ocr_text: str = "",
        task_type: str = "auto",
    ) -> VLMResult:
        raw_response = await self.client.analyze(
            image_path=image_path,
            prompt=self._build_prompt(user_question, ocr_text, task_type),
        )
        return self._parse_response(raw_response)

    def _build_prompt(self, user_question: Optional[str], ocr_text: str, task_type: str) -> str:
        return f"""
你是一个图像理解模块，需要分析用户上传的图片。

任务类型：
{task_type}

用户问题：
{user_question or "用户没有提出具体问题，请主动理解图片内容。"}

PaddleOCR 已识别出的文字：
{ocr_text if ocr_text else "OCR 未识别到有效文字。"}

请完成以下任务：
1. 判断图片类型。
2. 用简洁准确的语言总结图片内容。
3. 提取图片中的关键信息。
4. 如果是论文截图或论文 Figure，请从科研阅读角度解释。
5. 如果是报错截图，请提取错误原因和可能解决方向。
6. 如果 OCR 内容可能有误，请指出不确定性。

只输出 JSON，不要输出 Markdown。格式：
{{
  "image_type": "paper_figure | paper_screenshot | formula_screenshot | table_screenshot | error_screenshot | ui_screenshot | daily_screenshot | chart_screenshot | unknown",
  "visual_summary": "图片整体内容摘要",
  "key_information": ["关键信息1", "关键信息2"],
  "potential_issues": ["可能的不确定性1"]
}}
""".strip()

    def _parse_response(self, raw_response: str) -> VLMResult:
        content = (raw_response or "").strip()
        data = _extract_json_object(content)
        if data:
            return VLMResult(
                visual_summary=str(data.get("visual_summary") or ""),
                image_type=str(data.get("image_type") or "unknown"),
                key_information=_string_list(data.get("key_information")),
                potential_issues=_string_list(data.get("potential_issues")),
            )
        return VLMResult(
            visual_summary=content,
            image_type="unknown",
            key_information=[],
            potential_issues=["视觉模型未返回结构化 JSON，已保留原始摘要。"] if content else [],
        )


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            cleaned = part.removeprefix("json").strip()
            if cleaned.startswith("{"):
                text = cleaned
                break
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
