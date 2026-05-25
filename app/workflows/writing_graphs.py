import logging
import os
import re

from langgraph.graph import END, StateGraph

from app.agents.base.agent import BaseAgent
from app.state.task_state import TaskState
from app.workflows.base import (
    AgentWorkflowState,
    BuildAgentInput,
    ProgressCallback,
    continue_or_end,
    make_agent_node,
)

logger = logging.getLogger(__name__)


# NOTE: build_kb_writing_graph and build_uploaded_file_writing_graph used to
# live here. They were dead-code paths — build_academic_writing_graph below
# already handles all four source modes (user_input_only / library / upload /
# upload_plus_library) via _prepare_academic_writing_node's source detection.
# All routing for "kb_writing_workflow" and "uploaded_file_writing_workflow"
# is now aliased to "academic_writing_workflow" at the schema layer.


def build_academic_writing_graph(
    *,
    workflow_name: str,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
):
    """Unified academic-writing graph.

    Source-mode dispatch happens in `_prepare_academic_writing_node`:
      - user_input_only  → writing_agent
      - upload(+library) → parse uploaded file → (rag_agent if library) → writing_agent
      - library          → rag_agent → collect_library_material → writing_agent

    The previous retrieval_agent + reading_agent pair is now a single
    rag_agent node — same downstream consumers, fewer hops."""
    builder = StateGraph(AgentWorkflowState)
    builder.add_node("prepare_academic_writing", _prepare_academic_writing_node)
    builder.add_node("parse_uploaded_file", _parse_uploaded_file_node(progress))
    builder.add_node("reading_file", _reading_uploaded_file_node)
    builder.add_node(
        "rag_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="rag_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )
    builder.add_node("collect_library_material", _collect_library_material_node(progress))
    builder.add_node(
        "writing_agent",
        make_agent_node(
            workflow_name=workflow_name,
            agent_name="writing_agent",
            agents=agents,
            build_agent_input=build_agent_input,
            progress=progress,
        ),
    )

    builder.set_entry_point("prepare_academic_writing")
    builder.add_conditional_edges(
        "prepare_academic_writing",
        _route_after_academic_prepare,
        {
            "parse_upload": "parse_uploaded_file",
            "retrieve_library": "rag_agent",
            "write": "writing_agent",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "parse_uploaded_file",
        continue_or_end,
        {
            "continue": "reading_file",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "reading_file",
        _route_after_uploaded_material,
        {
            "retrieve_library": "rag_agent",
            "write": "writing_agent",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "rag_agent",
        continue_or_end,
        {
            "continue": "collect_library_material",
            "end": END,
        },
    )
    builder.add_edge("collect_library_material", "writing_agent")
    builder.add_edge("writing_agent", END)
    return builder.compile()


async def _prepare_academic_writing_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    user_goal = task_state.user_goal
    user_material = _extract_user_provided_material(user_goal)
    writing_followup = _looks_like_writing_followup(user_goal, task_state)
    if not user_material and task_state.working_memory.get("task_relation") == "continue_task":
        task_ctx = task_state.working_memory.get("active_task_context") or {}
        previous_content = str(task_ctx.get("subject_content") or "").strip()
        if previous_content:
            user_material = previous_content
            task_state.working_memory["writing_followup"] = True
    if not user_material and writing_followup:
        previous = task_state.working_memory.get("current_writing_context", {})
        previous_content = str(previous.get("content") or "").strip()
        if previous_content:
            user_material = previous_content
            task_state.working_memory["writing_followup"] = True
    if user_material:
        task_state.working_memory["user_provided_writing_material"] = user_material
        task_state.working_memory["writing_source_policy"] = "rewrite_user_text_first"

    docs = _select_uploaded_documents(task_state)
    task_state.working_memory["uploaded_writing_docs"] = docs
    wants_library = _wants_library_writing(user_goal)
    has_uploads = bool(docs)

    if writing_followup and not _explicitly_requests_upload(user_goal) and not wants_library:
        source_mode = "user_input_only"
        agent_names = ["writing_agent"]
    elif has_uploads and wants_library:
        source_mode = "upload_plus_library"
        agent_names = ["rag_agent", "writing_agent"]
    elif has_uploads:
        source_mode = "upload"
        agent_names = ["writing_agent"]
    elif wants_library:
        source_mode = "library"
        agent_names = ["rag_agent", "writing_agent"]
    elif user_material:
        source_mode = "user_input_only"
        agent_names = ["writing_agent"]
    else:
        source_mode = "user_input_only"
        agent_names = ["writing_agent"]

    task_state.working_memory["writing_source"] = source_mode
    task_state.working_memory["library_qa_mode"] = source_mode in {"library", "upload_plus_library"}
    task_state.working_memory["writing_material_summary"] = _build_source_mode_summary(source_mode, user_material, docs)
    state["agent_names"] = agent_names
    state["total_agents"] = len(agent_names)
    state["done_agents"] = 0
    return state


def _route_after_academic_prepare(state: AgentWorkflowState) -> str:
    if state.get("stopped"):
        return "end"
    source_mode = state["task_state"].working_memory.get("writing_source", "user_input_only")
    if source_mode in {"upload", "upload_plus_library"}:
        return "parse_upload"
    if source_mode == "library":
        return "retrieve_library"
    return "write"


def _route_after_uploaded_material(state: AgentWorkflowState) -> str:
    if state.get("stopped"):
        return "end"
    source_mode = state["task_state"].working_memory.get("writing_source", "upload")
    if source_mode == "upload_plus_library":
        return "retrieve_library"
    return "write"


def _collect_library_material_node(progress: ProgressCallback):
    async def node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        source_mode = task_state.working_memory.get("writing_source", "library")
        existing_chunks = list(task_state.working_memory.get("writing_material_chunks", []))
        retrieval = task_state.agent_outputs.get("retrieval_agent", {}).get("result", {})
        reading = task_state.agent_outputs.get("reading_agent", {}).get("result", {})
        library_chunks = retrieval.get("retrieved_chunks", [])
        if source_mode == "upload_plus_library":
            task_state.working_memory["writing_material_chunks"] = existing_chunks + library_chunks
            summary = _build_uploaded_summary(
                existing_chunks,
                task_state.working_memory.get("user_provided_writing_material", ""),
            )
            library_summary = _build_retrieval_summary(retrieval)
            if reading.get("answer"):
                library_summary = (library_summary + "\n\nLibrary synthesis:\n" + str(reading.get("answer"))[:1200]).strip()
            task_state.working_memory["writing_material_summary"] = (
                summary + "\n\n" + library_summary
            ).strip()
        else:
            task_state.working_memory["writing_material_chunks"] = library_chunks
            summary = _build_retrieval_summary(retrieval)
            if reading.get("answer"):
                summary = (summary + "\n\nReading synthesis:\n" + str(reading.get("answer"))[:1200]).strip()
            task_state.working_memory["writing_material_summary"] = summary
        task_state.current_stage = "writing_material_ready"
        await progress("collect_library_material", "Preparing writing materials...", 68)
        return state

    return node


async def _validate_uploaded_file_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    task_state.working_memory.pop("library_qa_mode", None)
    task_state.working_memory["writing_source"] = "upload"
    user_material = _extract_user_provided_material(task_state.user_goal)
    if user_material:
        task_state.working_memory["user_provided_writing_material"] = user_material
        task_state.working_memory["writing_source_policy"] = "rewrite_user_text_first"
    docs = _select_uploaded_documents(task_state)
    task_state.working_memory["uploaded_writing_docs"] = docs
    if not docs and not user_material:
        task_state.add_error("No readable uploaded writing material found.")
        task_state.record_agent_output(
            "writing_agent",
            {"result": {"reply": "没有找到可用于写作的上传文件，请先上传 PDF/TXT/Markdown/PPTX 等素材。"}},
        )
        state["stopped"] = True
    state["agent_names"] = ["writing_agent"]
    state["total_agents"] = 1
    state["done_agents"] = 0
    return state


def _parse_uploaded_file_node(progress: ProgressCallback):
    async def node(state: AgentWorkflowState) -> AgentWorkflowState:
        task_state = state["task_state"]
        docs = task_state.working_memory.get("uploaded_writing_docs", [])
        chunks = _extract_uploaded_chunks(docs)
        user_material = task_state.working_memory.get("user_provided_writing_material", "")
        if user_material:
            chunks = _filter_chunks_for_user_material(chunks, user_material)
        task_state.working_memory["uploaded_writing_chunks"] = chunks
        source_mode = task_state.working_memory.get("writing_source", "")
        if not chunks and not user_material and source_mode != "upload_plus_library":
            task_state.add_error("Uploaded files were found, but no writing chunks could be extracted.")
            task_state.record_agent_output(
                "writing_agent",
                {"result": {"reply": "已找到上传文件，但没有解析出可用于写作的正文内容。"}},
            )
            state["stopped"] = True
        await progress("parse_uploaded_file", "Parsing uploaded writing materials...", 55)
        return state

    return node


async def _reading_uploaded_file_node(state: AgentWorkflowState) -> AgentWorkflowState:
    task_state = state["task_state"]
    chunks = task_state.working_memory.get("uploaded_writing_chunks", [])
    task_state.working_memory["writing_material_chunks"] = chunks
    user_material = task_state.working_memory.get("user_provided_writing_material", "")
    task_state.working_memory["writing_material_summary"] = _build_uploaded_summary(chunks, user_material)
    task_state.current_stage = "uploaded_writing_material_ready"
    return state


def _select_uploaded_documents(task_state: TaskState) -> list[dict]:
    allowed_paths = _extract_writing_material_paths(task_state.user_goal)
    if not allowed_paths and not _explicitly_requests_upload(task_state.user_goal):
        return []
    docs = [
        p for p in task_state.working_memory.get("stored_papers", [])
        if p.get("source") == "upload" and p.get("local_path")
    ]
    if allowed_paths:
        docs = [
            p for p in docs
            if os.path.normpath(p.get("local_path", "")) in allowed_paths
        ]
    return [
        p for p in docs
        if p.get("local_path") and os.path.exists(p.get("local_path", ""))
    ]


def _extract_uploaded_chunks(uploaded_docs: list[dict]) -> list[dict]:
    try:
        from app.tools.pdf.backends import extract, extract_any
    except Exception as exc:
        logger.warning("Upload extraction unavailable for writing workflow: %s", exc)
        return []

    chunks: list[dict] = []
    for doc_idx, paper in enumerate(uploaded_docs[:5], start=1):
        path = paper.get("local_path", "")
        if not path or not os.path.exists(path):
            continue
        title = paper.get("title") or os.path.basename(path)
        try:
            if os.path.splitext(path)[1].lower() == ".pdf":
                extracted = extract(path)
            else:
                extracted = extract_any(path)
        except Exception as exc:
            logger.warning("Failed to extract uploaded writing material '%s': %s", title, exc)
            continue
        for chunk_idx, raw in enumerate((extracted.get("rag_chunks") or [])[:6], start=1):
            text = raw.get("text") or raw.get("content") or ""
            if not text.strip():
                continue
            metadata = raw.get("metadata") or {}
            chunks.append({
                "chunk_id": f"upload_{doc_idx:02d}_chunk_{chunk_idx:03d}",
                "title": title,
                "content": text,
                "section": metadata.get("section", ""),
                "page": metadata.get("page", 0),
                "metadata": {
                    **metadata,
                    "source_type": "upload",
                    "file_name": title,
                    "file_path": path,
                },
            })
    return chunks


def _extract_writing_material_paths(user_goal: str) -> set[str]:
    marker = "WRITING_MATERIAL_PATHS="
    if marker not in user_goal:
        return set()
    tail = user_goal.split(marker, 1)[1].splitlines()[0]
    return {
        os.path.normpath(item.strip())
        for item in tail.split("|")
        if item.strip()
    }


def _explicitly_requests_upload(user_goal: str) -> bool:
    source_match = re.search(r"WRITING_SOURCE=([^\s\r\n]+)", user_goal, re.IGNORECASE)
    if source_match:
        source = source_match.group(1).strip().lower()
        if source in {"upload", "both", "upload_plus_library"}:
            return True
    return bool(_extract_writing_material_paths(user_goal)) or any(token in user_goal.lower() for token in [
        "上传",
        "上传文件",
        "上传的文件",
        "uploaded",
        "uploaded file",
        "this file",
    ])


def _looks_like_writing_followup(user_goal: str, task_state: TaskState) -> bool:
    if not (task_state.working_memory.get("current_writing_context") or {}).get("content"):
        return False
    q = user_goal.lower().strip()
    markers = [
        "继续扩写", "继续写", "写多点", "写长一点", "太短", "扩写一下", "再扩写",
        "润色一下", "再润色", "改写一下", "继续改", "写成", "写为", "整理成", "成一段", "一段话",
        "上一版", "上面这段", "刚才那段", "这段",
        "make it longer", "expand it", "continue writing", "polish it", "revise it", "rewrite it", "previous draft",
    ]
    return any(marker in q for marker in markers)


def _wants_library_writing(user_goal: str) -> bool:
    source_match = re.search(r"WRITING_SOURCE=([^\s\r\n]+)", user_goal, re.IGNORECASE)
    if source_match:
        source = source_match.group(1).strip().lower()
        if source in {"user_input", "upload", "rewrite"} and "WRITING_USE_LIBRARY=1" not in user_goal:
            return False
        if source in {"library", "both", "upload_plus_library"}:
            return True
    markers = [
        "WRITING_USE_LIBRARY=1",
        "WRITING_USE_LIBRARY=true",
        "WRITING_SOURCE=library",
        "WRITING_SOURCE=both",
        "WRITING_SOURCE=upload_plus_library",
    ]
    lower_goal = user_goal.lower()
    if any(marker.lower() in lower_goal for marker in markers):
        return True
    return any(token in user_goal for token in [
        "知识库", "文献库", "资料库", "检索",
        "knowledge base", "literature library", "my library",
    ])


def _build_source_mode_summary(source_mode: str, user_material: str, docs: list[dict]) -> str:
    labels = {
        "user_input_only": "Using only the user's input text and instruction.",
        "library": "Using retrieved knowledge-base materials.",
        "upload": "Using uploaded writing materials.",
        "upload_plus_library": "Using both uploaded writing materials and retrieved knowledge-base materials.",
    }
    parts = [labels.get(source_mode, source_mode)]
    if user_material:
        parts.append("User-provided text is the primary writing target.")
    if docs:
        names = [doc.get("title") or os.path.basename(doc.get("local_path", "")) for doc in docs[:5]]
        parts.append("Uploaded files: " + ", ".join(name for name in names if name))
    return " ".join(parts)


def _build_retrieval_summary(retrieval_result: dict) -> str:
    metadata = retrieval_result.get("metadata", {})
    contexts = retrieval_result.get("contexts", [])
    lib_names = retrieval_result.get("lib_names", [])
    parts = []
    if metadata.get("chunk_count") is not None:
        parts.append(f"Retrieved {metadata.get('chunk_count')} chunks")
    if lib_names:
        parts.append("Libraries: " + ", ".join(str(x) for x in lib_names))
    if contexts:
        parts.append("Top contexts:\n" + "\n\n".join(str(c)[:800] for c in contexts[:3]))
    return "\n".join(parts)


def _extract_user_provided_material(user_goal: str) -> str:
    markers = [
        "我提供的素材：",
        "用户输入：",
        "待处理文本：",
        "写作原文：",
        "INPUT_TEXT:",
        "USER_MATERIAL:",
    ]
    marker = next((m for m in markers if m in user_goal), "")
    if not marker:
        return ""
    text = user_goal.rsplit(marker, 1)[1].strip()
    if len(text) < 2:
        return ""
    return text


def _filter_chunks_for_user_material(chunks: list[dict], user_material: str) -> list[dict]:
    material_terms = _material_terms(user_material)
    if not material_terms:
        return chunks
    filtered: list[dict] = []
    for chunk in chunks:
        content = str(chunk.get("content") or "")
        chunk_terms = _material_terms(content)
        overlap = material_terms & chunk_terms
        if len(overlap) >= 2:
            filtered.append(chunk)
    return filtered


def _material_terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
    terms.update(chinese[i:i + 2] for i in range(max(len(chinese) - 1, 0)))
    stopwords = {
        "一个", "一种", "进行", "通过", "可以", "以及", "中的", "对于",
        "图像", "任务", "模型", "方法", "过程", "结果", "本文",
    }
    return {term for term in terms if term not in stopwords}


def _build_uploaded_summary(chunks: list[dict], user_material: str = "") -> str:
    titles = []
    for chunk in chunks:
        title = chunk.get("title", "")
        if title and title not in titles:
            titles.append(title)
    parts = []
    if user_material:
        parts.append("User-provided material is the primary rewrite source.")
    if chunks:
        parts.append(f"Parsed {len(chunks)} relevant chunks from uploaded files: " + ", ".join(titles[:5]))
    elif user_material:
        parts.append("No uploaded chunks were used because they did not match the user-provided material.")
    return " ".join(parts)
