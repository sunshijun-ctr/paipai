import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from app.agents.writing.prompts import WRITING_SYSTEM_PROMPT, WRITING_USER_TEMPLATE
from app.schemas.writing import WritingAgentInput, WritingAgentOutput
from app.services.llm import BaseLLMProvider, LLMMessage

logger = logging.getLogger(__name__)


class WritingService:
    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def generate(self, payload: WritingAgentInput) -> WritingAgentOutput:
        prompt = WRITING_USER_TEMPLATE.format(
            user_query=payload.user_query,
            writing_task_type=payload.writing_task_type,
            constraints=payload.constraints.model_dump(),
            retrieval_summary=payload.retrieval_summary or "",
            user_provided_material=payload.user_provided_material or "",
            source_policy=payload.source_policy or "",
            retrieved_chunks=json.dumps(
                [c.model_dump() for c in payload.retrieved_chunks],
                ensure_ascii=False,
                indent=2,
            ),
            user_extra_instruction=payload.user_extra_instruction or "",
        )

        try:
            response = await self.llm.complete(
                messages=[LLMMessage(role="user", content=prompt)],
                system=WRITING_SYSTEM_PROMPT,
                temperature=0.45,
            )
            raw = _parse_jsonish(response.content)
            return WritingAgentOutput(**_normalize_output(raw, payload))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.warning("WritingAgent JSON parsing failed: %s", exc)
            return self._fallback_output(payload, locals().get("response", None).content if locals().get("response") else "")
        except Exception as exc:
            logger.warning("WritingAgent LLM call failed: %s", exc)
            return self._insufficient_material_output(payload)

    @staticmethod
    def _fallback_output(payload: WritingAgentInput, content: str) -> WritingAgentOutput:
        return WritingAgentOutput(
            task_type=payload.writing_task_type,
            title=None,
            content=content.strip() or "当前未能生成有效写作内容。",
            citations=[],
            material_usage_summary="模型未返回可校验的结构化 JSON，已保留原始文本输出。",
            limitations=["输出结构化解析失败，引用信息未能可靠抽取。"],
            suggested_next_steps=["请重新生成，或补充更明确的写作要求。"],
        )

    @staticmethod
    def _insufficient_material_output(payload: WritingAgentInput) -> WritingAgentOutput:
        return WritingAgentOutput(
            task_type=payload.writing_task_type,
            title=None,
            content="当前材料不足以支撑完整论文写作段落，建议补充更多文献或扩大检索范围。",
            citations=[],
            material_usage_summary="未获得可用于写作的可靠材料。",
            limitations=["缺少可引用的检索片段或上传文档片段。"],
            suggested_next_steps=["先检索或上传相关论文，再执行写作任务。"],
        )


def _normalize_output(raw: dict[str, Any], payload: WritingAgentInput) -> dict[str, Any]:
    """Accept common LLM JSON variants and coerce them to WritingAgentOutput."""
    data = dict(raw or {})
    data.setdefault("task_type", payload.writing_task_type)
    data.setdefault("title", None)
    data["content"] = str(data.get("content") or "").strip()
    data["material_usage_summary"] = str(data.get("material_usage_summary") or "").strip()
    data["limitations"] = _as_string_list(data.get("limitations"))
    data["suggested_next_steps"] = _as_string_list(data.get("suggested_next_steps"))
    data["citations"] = _normalize_citations(data.get("citations"), payload)
    return data


def _parse_jsonish(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])

    last_exc: Exception | None = None
    for candidate in candidates:
        for repaired in (candidate, _escape_invalid_json_backslashes(candidate)):
            try:
                parsed = json.loads(repaired)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError as exc:
                last_exc = exc
    raise json.JSONDecodeError(str(last_exc or "invalid json"), text, 0)


def _escape_invalid_json_backslashes(text: str) -> str:
    # LLMs often put raw LaTeX such as \lambda in JSON strings; JSON only allows
    # a small set of backslash escapes, so preserve the literal slash.
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)


def _as_string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _normalize_citations(value: Any, payload: WritingAgentInput) -> list[dict[str, Any]]:
    chunk_map = {chunk.chunk_id: chunk for chunk in payload.retrieved_chunks}
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    citations: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        if isinstance(item, str):
            chunk_id = item.strip()
            chunk = chunk_map.get(chunk_id)
            if not chunk:
                continue
            is_upload = chunk.metadata.get("source_type") == "upload"
            citations.append({
                "ref_id": f"[U{idx}]" if is_upload else f"[{idx}]",
                "chunk_id": chunk.chunk_id,
                "title": chunk.title,
                "year": chunk.year,
                "page": chunk.page,
            })
            continue
        if isinstance(item, dict):
            chunk_id = str(item.get("chunk_id") or "").strip()
            if not chunk_id or chunk_id not in chunk_map:
                continue
            chunk = chunk_map[chunk_id]
            is_upload = chunk.metadata.get("source_type") == "upload"
            citations.append({
                "ref_id": item.get("ref_id") or (f"[U{idx}]" if is_upload else f"[{idx}]"),
                "chunk_id": chunk_id,
                "title": item.get("title") or chunk.title,
                "year": item.get("year") or chunk.year,
                "page": item.get("page") or chunk.page,
            })
    return citations
