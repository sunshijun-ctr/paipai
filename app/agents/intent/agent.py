import logging
import re
from typing import Any

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentOutput, AgentStatus
from app.schemas.workflow import normalize_workflow_intent
from app.services.llm import BaseLLMProvider, LLMMessage
from app.session.context import SessionContext
from app.state.task_state import TaskState

logger = logging.getLogger(__name__)


def zh(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


_SYSTEM_PROMPT = """\
You are the Intent Agent for a research assistant.
Your job is to route the user's request. You are not a forced workflow matcher.
Do not answer the user. Do not call tools. Do not plan full agent chains.

Return strict JSON only:
{
  "route": "workflow_task | open_task | clarify",
  "intent": "",
  "workflow": "",
  "confidence": 0.0,
  "need_clarification": false,
  "clarification_question": null,
  "reason": "",
  "missing_slots": [],
  "suggested_agent": null
}

Available workflows:
1. paper_search_workflow: search/find papers, including requests that may later download papers.
2. conversation_summary_workflow: summarize or organize the current conversation/session.
3. question_answer_workflow: answer questions about uploaded files, papers, the literature library, or knowledge base.
4. image_understanding_workflow: analyze an uploaded image and answer questions about it.
5. library_ingest_workflow: add uploaded/downloaded/local files into the literature library/knowledge base.
6. academic_writing_workflow: academic writing transformation, including expanding, polishing, supplementing, paraphrasing, semantic rewriting, style imitation, literature-style writing, or writing from user input/uploads/library.
7. kb_writing_workflow: legacy alias for writing based on knowledge base.
8. uploaded_file_writing_workflow: legacy alias for writing based on uploaded files.
9. note_workflow: create, save, update, delete, search, list, organize, or embed notes.
10. chat_workflow: lightweight ordinary conversation or clarification.
11. general_agent_workflow: open-ended tasks handled by a Plan-and-Action General Agent.

Routing rules:
- Return route="workflow_task" only when a workflow fully covers the user's goal, required inputs are present, and execution should start immediately.
- Return route="open_task" when the request is open-ended, mixed, exploratory, advisory, architectural, research-design oriented, or not fully covered by one workflow.
- Return route="clarify" when the target is too vague and execution would be risky.
- If no workflow fully covers the request, do not choose the closest workflow. Use route="open_task", workflow="general_agent_workflow", intent="general_open_task".
- Workflows are tools. Open mixed tasks should go to the General Agent, which may use workflows as subtasks.
- Comparing, contrasting, differentiating, evaluating pros/cons, or synthesizing differences across multiple papers is an open task.
  Even if the papers are in the knowledge base, use route="open_task" and workflow="general_agent_workflow";
  retrieval/reading can be used later as subtasks.

Important:
- If the message contains IMAGE_PATH=, choose image_understanding_workflow.
- If the message contains WRITING_SOURCE=library, choose kb_writing_workflow.
- If the message contains WRITING_SOURCE=upload or WRITING_SOURCE=rewrite, choose uploaded_file_writing_workflow.
- If the message contains ACADEMIC_WRITING=1, choose academic_writing_workflow.
- For requests to expand, polish, supplement, paraphrase, semantically rewrite, imitate style, or generate academic text, choose academic_writing_workflow.
- The question_answer_workflow itself decides uploaded-file versus library source.
- For unclear requests, set route="clarify", need_clarification=true, and ask one short clarification question.

Open task examples:
- "这个研究方向怎么做比较好?"
- "帮我设计一个更灵活的 agent 架构"
- "这个 idea 靠谱吗?"
- "帮我设计研究方案，顺便找几篇论文支撑"
- "这段代码为什么看起来怪?"
"""

LAST_WORKFLOW_OUTPUT_TOKEN_LIMIT = 500


class IntentAgent(BaseAgent):
    name = "intent_agent"
    description = "Classifies user requests into workflow routes."

    def __init__(self, llm: BaseLLMProvider) -> None:
        self.llm = llm

    async def run(self, agent_input: AgentInput, state: TaskState) -> AgentOutput:
        prompt: str = agent_input.input_data.get("user_query", agent_input.user_goal)
        raw_query: str = agent_input.user_goal
        session_context = _coerce_session_context(agent_input.input_data.get("session_context"), state.session_id)

        try:
            raw_result: dict[str, Any] = await self.llm.complete_json(
                messages=[LLMMessage(role="user", content=prompt)],
                system=_build_intent_system_prompt(session_context),
            )
        except Exception as exc:
            logger.warning("IntentAgent LLM call failed (%s), using fallback workflow.", exc)
            raw_result = _fallback_intent(raw_query)

        result = _normalize_route_result(normalize_workflow_intent(raw_result))
        result.update(_derived_legacy_fields(raw_query, result))

        logger.info("Intent result: %s", result)
        state.update_stage("intent_recognized", self.name)

        return AgentOutput(
            task_id=agent_input.task_id,
            session_id=agent_input.session_id,
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            result=result,
            next_suggestion=f"dispatch_to_{result.get('workflow', 'chat_workflow')}",
        )


def _build_intent_system_prompt(session_context: SessionContext | None = None) -> str:
    if not session_context:
        return _SYSTEM_PROMPT

    context_parts: list[str] = []
    if session_context.current_task:
        context_parts.append(f"current_task: {session_context.current_task}")
    if session_context.last_workflow_output:
        context_parts.append(
            "last_workflow_output:\n"
            + _limit_by_token_estimate(session_context.last_workflow_output, LAST_WORKFLOW_OUTPUT_TOKEN_LIMIT)
        )
    if session_context.active_entities:
        context_parts.append("active_entities: " + ", ".join(session_context.active_entities))
    if not context_parts:
        return _SYSTEM_PROMPT

    return "Session context for routing:\n" + "\n\n".join(context_parts) + "\n\n" + _SYSTEM_PROMPT


def _coerce_session_context(value: Any, session_id: str) -> SessionContext | None:
    if isinstance(value, SessionContext):
        return value
    if isinstance(value, dict):
        return SessionContext.from_dict(value, session_id=session_id)
    return None


def _limit_by_token_estimate(text: str, token_limit: int) -> str:
    char_limit = max(token_limit * 4, 0)
    content = str(text or "")
    if len(content) <= char_limit:
        return content
    return content[:char_limit].rstrip()


def _fallback_intent(query: str) -> dict[str, Any]:
    q = query.lower()
    source = _extract_writing_source(query)
    if "image_path=" in q:
        return _workflow("image_understanding", "image_understanding_workflow", "uploaded image needs analysis")
    if "academic_writing=1" in q:
        return _workflow("paper_writing", "academic_writing_workflow", "academic writing request")
    if source == "library":
        return _workflow("paper_writing", "academic_writing_workflow", "explicit library writing source")
    if source in {"upload", "rewrite"}:
        return _workflow("paper_writing", "academic_writing_workflow", "explicit upload/rewrite writing source")
    if _looks_like_library_ingest(q):
        return _workflow("library_ingest", "library_ingest_workflow", "user wants to store material in library")
    if _looks_like_note_task(q):
        return _workflow("note", "note_workflow", "note operation")
    if _looks_like_summary_task(q):
        return _workflow("conversation_summary", "conversation_summary_workflow", "conversation summary")
    if _looks_like_writing_task(q):
        if _looks_like_upload_reference(q):
            return _workflow("paper_writing", "academic_writing_workflow", "writing from uploaded file")
        if _looks_like_library_reference(q):
            return _workflow("paper_writing", "academic_writing_workflow", "writing from knowledge base")
        return _workflow("paper_writing", "academic_writing_workflow", "academic writing request")
    if _looks_like_comparison_task(q):
        return _open_task("comparison/synthesis task; general agent should orchestrate retrieval and reading", 0.82)
    if _looks_like_research_search(q):
        return _workflow("paper_search", "paper_search_workflow", "paper search/download/reading request")
    if _looks_like_qa(q) and (_looks_like_upload_reference(q) or _looks_like_library_reference(q)):
        return _workflow("question_answer", "question_answer_workflow", "question about papers/files/library")
    if _looks_like_light_chat(q):
        return _workflow("chat", "chat_workflow", "lightweight conversation")
    return _open_task("open-ended request; use plan-and-action general agent")


def _workflow(intent: str, workflow: str, reason: str, confidence: float = 0.72) -> dict[str, Any]:
    return {
        "route": "workflow_task",
        "intent": intent,
        "workflow": workflow,
        "confidence": confidence,
        "need_clarification": False,
        "clarification_question": None,
        "reason": reason,
    }


def _open_task(reason: str, confidence: float = 0.72) -> dict[str, Any]:
    return {
        "route": "open_task",
        "intent": "general_open_task",
        "workflow": "general_agent_workflow",
        "confidence": confidence,
        "need_clarification": False,
        "clarification_question": None,
        "reason": reason,
        "missing_slots": [],
        "suggested_agent": "general_agent",
    }


def _normalize_route_result(result: dict[str, Any]) -> dict[str, Any]:
    route = str(result.get("route") or "").strip()
    workflow = str(result.get("workflow") or "")
    intent = str(result.get("intent") or "").strip().lower()
    if result.get("need_clarification"):
        result["route"] = "clarify"
        result["workflow"] = "chat_workflow"
        result["user_intent"] = "general_chat"
        return result
    if intent in {"compare_papers", "paper_comparison", "compare", "contrast_papers", "synthesize_papers"}:
        result["route"] = "open_task"
        result["workflow"] = "general_agent_workflow"
        result["intent"] = "general_open_task"
        result["user_intent"] = "general_open_task"
        result["suggested_agent"] = "general_agent"
        result["reason"] = (
            str(result.get("reason") or "").strip()
            + " Routed to General Agent because paper comparison requires synthesis, not simple QA."
        ).strip()
        return result
    if route == "open_task":
        result["workflow"] = "general_agent_workflow"
        result["intent"] = result.get("intent") or "general_open_task"
        result["user_intent"] = "general_open_task"
        result["suggested_agent"] = "general_agent"
        return result
    if workflow == "general_agent_workflow":
        result["route"] = "open_task"
        result["intent"] = result.get("intent") or "general_open_task"
        result["user_intent"] = "general_open_task"
        result["suggested_agent"] = "general_agent"
        return result
    result["route"] = route or "workflow_task"
    return result


def _derived_legacy_fields(query: str, result: dict[str, Any]) -> dict[str, Any]:
    workflow = result.get("workflow", "chat_workflow")
    fields: dict[str, Any] = {
        "search_query": _extract_english_keywords(query) if workflow == "paper_search_workflow" else "",
        "title_search": _looks_like_specific_title(query),
        "target_indices": _extract_indices(query),
        "target_keywords": "",
        "sort_by": _infer_sort_by(query),
        "writing_task_type": _infer_writing_task_type(query.lower()),
        "constraints": {
            "language": "en" if _looks_english_dominant(query) else "zh",
            "style": "academic",
            "length": "medium",
            "citation_required": True,
        },
    }
    if workflow == "paper_search_workflow":
        fields["download_target"] = "specific" if fields["target_indices"] else ""
    if workflow in {"academic_writing_workflow", "kb_writing_workflow", "uploaded_file_writing_workflow"}:
        fields["need_retrieval"] = workflow == "kb_writing_workflow"
        fields["use_retrieval"] = workflow == "kb_writing_workflow"
    return fields


def _looks_like_library_ingest(q: str) -> bool:
    return any(w in q for w in [
        zh("\\u52a0\\u5165\\u77e5\\u8bc6\\u5e93"),
        zh("\\u5b58\\u5165\\u77e5\\u8bc6\\u5e93"),
        zh("\\u52a0\\u5165\\u6587\\u732e\\u5e93"),
        zh("\\u5b58\\u5165\\u6587\\u732e\\u5e93"),
        zh("\\u5165\\u5e93"),
        "add to library", "save to library",
    ])


def _looks_like_note_task(q: str) -> bool:
    return any(w in q for w in [zh("\\u7b14\\u8bb0"), "note", "notes"])


def _looks_like_summary_task(q: str) -> bool:
    stripped = q.strip()
    if stripped in {zh("\\u603b\\u7ed3"), "summarize"}:
        return True
    return any(w in q for w in [
        zh("\\u603b\\u7ed3\\u4e0a"),
        zh("\\u603b\\u7ed3\\u6211\\u4eec"),
        zh("\\u603b\\u7ed3\\u6574\\u4e2a\\u5bf9\\u8bdd"),
        zh("\\u603b\\u7ed3\\u5bf9\\u8bdd"),
        zh("\\u603b\\u7ed3\\u4e00\\u4e0b"),
        zh("\\u505a\\u4e2a\\u603b\\u7ed3"),
        zh("\\u6574\\u7406\\u521a\\u624d"),
        zh("\\u5f53\\u524d\\u4f1a\\u8bdd"),
        "summarize", "wrap up",
    ])


def _looks_like_writing_task(q: str) -> bool:
    return any(w in q for w in [
        zh("\\u5199"), zh("\\u64b0\\u5199"), zh("\\u6539\\u5199"),
        zh("\\u6da6\\u8272"), zh("\\u6269\\u5199"), zh("\\u7efc\\u8ff0"),
        zh("\\u8865\\u5145"), zh("\\u8bed\\u4e49\\u8f6c\\u6362"), zh("\\u4eff\\u5199"),
        zh("\\u6a21\\u4eff"), zh("\\u964d\\u91cd"),
        zh("\\u4ecb\\u7ecd"), zh("\\u6458\\u8981"), zh("\\u5f15\\u8a00"),
        zh("\\u7ed3\\u8bba"),
        "related work", "literature review", "write", "draft", "rewrite", "polish", "expand",
        "supplement", "paraphrase", "imitate", "style imitation", "abstract", "introduction", "conclusion",
    ])


def _looks_like_upload_reference(q: str) -> bool:
    return any(w in q for w in [
        zh("\\u4e0a\\u4f20"), zh("\\u8fd9\\u4e2a\\u6587\\u4ef6"),
        zh("\\u8fd9\\u4e2apdf"), zh("\\u8fd9\\u4e2a pdf"),
        zh("\\u4e0a\\u4f20\\u7684\\u6587\\u4ef6"),
        "uploaded", "this pdf", "this file",
    ])


def _looks_like_library_reference(q: str) -> bool:
    return any(w in q for w in [
        zh("\\u77e5\\u8bc6\\u5e93"), zh("\\u6587\\u732e\\u5e93"),
        zh("\\u8d44\\u6599\\u5e93"), "my library", "knowledge base", "literature library",
    ])


def _looks_like_research_search(q: str) -> bool:
    return any(w in q for w in [
        zh("\\u641c\\u7d22"), zh("\\u67e5\\u627e"), zh("\\u627e"),
        zh("\\u4e0b\\u8f7d"), zh("\\u8bba\\u6587"), zh("\\u6587\\u732e"),
        "paper", "papers", "search", "find", "download",
    ])


def _looks_like_qa(q: str) -> bool:
    return "?" in q or zh("\\uff1f") in q or any(w in q for w in [
        zh("\\u4ec0\\u4e48"), zh("\\u600e\\u4e48"), zh("\\u4e3a\\u4ec0\\u4e48"),
        zh("\\u89e3\\u91ca"), zh("\\u56de\\u7b54"), zh("\\u8bb2\\u4e86"),
        zh("\\u65b9\\u6cd5"), zh("\\u5b9e\\u9a8c"), zh("\\u7ed3\\u679c"),
        "how", "what", "why", "explain",
    ])


def _looks_like_comparison_task(q: str) -> bool:
    return any(w in q for w in [
        zh("\\u5bf9\\u6bd4"), zh("\\u6bd4\\u8f83"), zh("\\u4e0d\\u540c"),
        zh("\\u5dee\\u5f02"), zh("\\u533a\\u522b"), zh("\\u5f02\\u540c"),
        zh("\\u4f18\\u7f3a\\u70b9"), zh("\\u4f18\\u52a3"),
        "compare", "comparison", "contrast", "difference", "differences",
        "different", "pros and cons", "versus", " vs ",
    ])


def _looks_like_light_chat(q: str) -> bool:
    stripped = q.strip()
    greetings = {
        "hi", "hello", "hey", "你好", "您好", "嗨", "在吗", "thanks", "thank you", "谢谢",
    }
    return stripped in greetings or len(stripped) <= 2


def _extract_writing_source(query: str) -> str:
    marker = "WRITING_SOURCE="
    if marker not in query:
        return ""
    value = query.split(marker, 1)[1].splitlines()[0].strip().lower()
    return value if value in {"upload", "library", "rewrite"} else ""


def _infer_writing_task_type(q: str) -> str:
    mapping = [
        (["abstract", zh("\\u6458\\u8981")], "abstract"),
        (["introduction", zh("\\u5f15\\u8a00")], "introduction"),
        (["related work", zh("\\u76f8\\u5173\\u5de5\\u4f5c")], "related_work"),
        (["background", zh("\\u80cc\\u666f")], "background"),
        (["method", zh("\\u65b9\\u6cd5")], "method_description"),
        (["experiment", zh("\\u5b9e\\u9a8c")], "experiment_analysis"),
        (["conclusion", zh("\\u7ed3\\u8bba")], "conclusion"),
        (["rewrite", "polish", zh("\\u6539\\u5199"), zh("\\u6da6\\u8272")], "academic_rewrite"),
        (["expand", zh("\\u6269\\u5199")], "expand_text"),
        (["summary", zh("\\u603b\\u7ed3")], "summarize_to_paragraph"),
        (["literature review", zh("\\u7efc\\u8ff0")], "literature_review"),
    ]
    for keys, value in mapping:
        if any(key in q for key in keys):
            return value
    return "literature_review"


def _looks_english_dominant(text: str) -> bool:
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in text)
    non_ascii = sum(not ch.isascii() for ch in text)
    return ascii_letters > non_ascii


def _extract_indices(query: str) -> list[int]:
    q = query.lower().strip()
    values: list[int] = []
    patterns = (
        r"\u7b2c\s*(\d+)\s*(?:\u7bc7|\u4e2a|\u6761|paper|papers)?",
        r"(?:\u4e0b\u8f7d|\u9009\u62e9|\u9009|download|select|choose|paper)\s*(\d+)\s*(?:\u7bc7|\u4e2a|\u6761|papers?)?",
    )
    for pattern in patterns:
        values.extend(int(x) for x in re.findall(pattern, q))
    if not values and re.fullmatch(r"\s*\d+(?:\s*(?:[,，、\s]|\band\b)\s*\d+)*\s*", q):
        values.extend(int(x) for x in re.findall(r"\d+", q))
    return [v for v in dict.fromkeys(values) if v > 0][:10]


def _infer_sort_by(query: str) -> str:
    q = query.lower()
    if any(w in q for w in [zh("\\u7ecf\\u5178"), zh("\\u91cd\\u8981"), zh("\\u5f15\\u7528"), "most cited", "classic", "foundational"]):
        return "citations"
    if any(w in q for w in [zh("\\u6700\\u65b0"), zh("\\u8fd1\\u4e09\\u5e74"), zh("\\u8fd1\\u4e24\\u5e74"), "recent", "latest"]):
        return "date"
    return "relevance"


def _looks_like_specific_title(query: str) -> bool:
    quoted = re.search(r"[\u300a\u201c\"'](.{4,120})[\u300b\u201d\"']", query)
    if quoted:
        return True
    ascii_words = re.findall(r"[A-Z][A-Za-z0-9\-]+", query)
    return len(ascii_words) >= 3


def _extract_english_keywords(query: str) -> str:
    q = query.lower()
    special_keyword_map = {
        "3dnr": "3DNR 3D noise reduction",
        "hdr": "HDR high dynamic range imaging",
    }
    special_tokens = [value for key, value in special_keyword_map.items() if key in q]
    if special_tokens:
        return " ".join(dict.fromkeys(special_tokens))

    english_words = [
        token
        for token in re.findall(r"(?<![A-Za-z0-9-])[A-Za-z0-9][A-Za-z0-9-]*(?![A-Za-z0-9-])", query)
        if any(ch.isalpha() for ch in token)
    ]
    if english_words:
        return " ".join(english_words[:8])
    keyword_map = {
        zh("\\u4f4e\\u5149"): "low-light image enhancement",
        zh("\\u56fe\\u50cf\\u589e\\u5f3a"): "image enhancement",
        zh("\\u53bb\\u566a"): "image denoising",
        zh("\\u9ad8\\u52a8\\u6001\\u8303\\u56f4"): "high dynamic range imaging",
        "hdr": "high dynamic range imaging",
        zh("\\u76ee\\u6807\\u68c0\\u6d4b"): "object detection",
        zh("\\u5c0f\\u76ee\\u6807"): "small object detection",
        zh("\\u6269\\u6563\\u6a21\\u578b"): "diffusion model",
        zh("\\u5927\\u6a21\\u578b"): "large language model",
        zh("\\u5927\\u8bed\\u8a00\\u6a21\\u578b"): "large language model",
        zh("\\u57fa\\u7840\\u6a21\\u578b"): "foundation model",
        zh("\\u68c0\\u7d22\\u589e\\u5f3a\\u751f\\u6210"): "retrieval augmented generation",
        zh("\\u77e5\\u8bc6\\u56fe\\u8c31"): "knowledge graph",
        zh("\\u5f3a\\u5316\\u5b66\\u4e60"): "reinforcement learning",
        zh("\\u8054\\u90a6\\u5b66\\u4e60"): "federated learning",
        zh("\\u591a\\u6a21\\u6001"): "multimodal learning",
        zh("\\u56fe\\u795e\\u7ecf\\u7f51\\u7edc"): "graph neural network",
        zh("\\u9065\\u611f"): "remote sensing",
    }
    tokens = [value for key, value in keyword_map.items() if key in query.lower()]
    return " ".join(dict.fromkeys(tokens)) if tokens else query
