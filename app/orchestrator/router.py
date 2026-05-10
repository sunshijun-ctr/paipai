import os
import re

from app.state.task_state import TaskState

_URL_RE = re.compile(r"https?://[^\s<>'\"）)】\]]+")


def resolve_workflow(intent_result: dict, user_query: str, state: TaskState) -> str:
    explicit_source = extract_writing_source(user_query)
    if "ACADEMIC_WRITING=1" in user_query:
        return "academic_writing_workflow"
    if explicit_source:
        return "academic_writing_workflow"
    if looks_like_writing_followup(user_query, state):
        return "academic_writing_workflow"

    workflow = intent_result.get("workflow") or _legacy_intent_to_workflow(intent_result.get("user_intent", ""))
    if workflow == "image_understanding_workflow":
        return workflow
    if looks_like_web_read_query(user_query):
        return "web_search_workflow"
    if workflow == "question_answer_workflow":
        return workflow
    if workflow:
        return workflow
    if looks_like_library_query(user_query):
        return "question_answer_workflow"
    if should_use_cached_library_context(user_query, state):
        return "question_answer_workflow"
    return "chat_workflow"


def workflow_agent_plan(workflow: str, user_query: str, state: TaskState) -> list[str]:
    if workflow == "paper_search_workflow":
        return ["literature_agent"]
    if workflow == "conversation_summary_workflow":
        return ["summary_agent"]
    if workflow == "image_understanding_workflow":
        return ["analyze_image", "chat_agent"]
    if workflow == "library_ingest_workflow":
        return []
    if workflow == "academic_writing_workflow":
        source = extract_writing_source(user_query) or "auto"
        state.working_memory["writing_source"] = source
        if source in {"library", "both", "upload_plus_library"}:
            state.working_memory["library_qa_mode"] = True
            return ["retrieval_agent", "reading_agent", "writing_agent"]
        return ["writing_agent"]
    if workflow == "kb_writing_workflow":
        state.working_memory["library_qa_mode"] = True
        state.working_memory["writing_source"] = "library"
        return ["retrieval_agent", "reading_agent", "writing_agent"]
    if workflow == "uploaded_file_writing_workflow":
        source = extract_writing_source(user_query) or "upload"
        state.working_memory["writing_source"] = source
        return ["writing_agent"]
    if workflow == "note_workflow":
        return ["note_agent"]
    if workflow == "general_agent_workflow":
        return ["general_agent"]
    if workflow == "question_answer_workflow":
        source = detect_qa_source(user_query, state)
        state.working_memory["qa_source"] = source
        if source == "web":
            state.working_memory["web_read_mode"] = True
            state.working_memory.pop("library_qa_mode", None)
            return ["reading_agent"]
        if source == "uploaded_file":
            return ["reading_agent"]
        state.working_memory["library_qa_mode"] = True
        if should_use_cached_library_context(user_query, state):
            state.working_memory["use_cached_library_context"] = True
            return ["reading_agent"]
        return ["retrieval_agent", "reading_agent"]
    if workflow == "web_search_workflow":
        state.working_memory["web_read_mode"] = True
        state.working_memory["qa_source"] = "web"
        state.working_memory.pop("library_qa_mode", None)
        return ["web_agent"]
    return ["chat_agent"]


def looks_like_writing_followup(query: str, state: TaskState) -> bool:
    if not (state.working_memory.get("current_writing_context") or {}).get("content"):
        return False
    q = query.lower().strip()
    markers = [
        "继续扩写",
        "继续写",
        "写多点",
        "写长一点",
        "太短",
        "扩写一下",
        "再扩写",
        "润色一下",
        "再润色",
        "改写一下",
        "继续改",
        "写成",
        "写为",
        "整理成",
        "成一段",
        "一段话",
        "上一版",
        "上面这段",
        "刚才那段",
        "这段",
        "make it longer",
        "expand it",
        "continue writing",
        "polish it",
        "revise it",
        "rewrite it",
        "previous draft",
    ]
    return any(marker in q for marker in markers)


def workflow_intent_label_key(workflow: str, fallback: str) -> str:
    return {
        "paper_search_workflow": "literature_search",
        "conversation_summary_workflow": "summarize_session",
        "question_answer_workflow": "library_qa",
        "web_search_workflow": "web_search",
        "image_understanding_workflow": "image_understanding",
        "library_ingest_workflow": "add_to_library",
        "academic_writing_workflow": "paper_writing",
        "kb_writing_workflow": "paper_writing",
        "uploaded_file_writing_workflow": "paper_writing",
        "note_workflow": fallback if "note" in fallback else "create_note",
        "general_agent_workflow": "general_open_task",
        "chat_workflow": "general_chat",
    }.get(workflow, fallback or "general_chat")


def paper_search_intent_from_query(query: str) -> str:
    q = query.lower()
    if "\u4e0b\u8f7d" in q or "download" in q:
        return "paper_download"
    if any(w in q for w in ["\u8bfb", "\u9605\u8bfb", "\u603b\u7ed3\u5185\u5bb9", "read", "summarize"]):
        return "research_literature_reading"
    return "literature_search"


def extract_writing_source(user_goal: str) -> str:
    marker = "WRITING_SOURCE="
    if marker not in user_goal:
        return ""
    value = user_goal.split(marker, 1)[1].splitlines()[0].strip().lower()
    return value if value in {"upload", "library", "rewrite", "user_input", "both", "upload_plus_library"} else ""


def has_uploaded_writing_docs(state: TaskState) -> bool:
    return any(
        p.get("source") == "upload" and p.get("local_path") and os.path.exists(p.get("local_path", ""))
        for p in state.working_memory.get("stored_papers", [])
    )


def looks_like_library_query(query: str) -> bool:
    q = query.lower()
    management_markers = [
        "\u52a0\u5165\u77e5\u8bc6\u5e93",
        "\u52a0\u5165\u6587\u732e\u5e93",
        "\u5b58\u5165\u77e5\u8bc6\u5e93",
        "\u5b58\u5165\u6587\u732e\u5e93",
        "\u5220\u9664\u77e5\u8bc6\u5e93",
        "\u6e05\u7a7a\u77e5\u8bc6\u5e93",
        "add to library",
        "save to library",
        "clear library",
        "delete library",
    ]
    if any(marker in q for marker in management_markers):
        return False

    query_markers = [
        "\u4ece\u6211\u7684\u77e5\u8bc6\u5e93",
        "\u5728\u6211\u7684\u77e5\u8bc6\u5e93",
        "\u6211\u7684\u77e5\u8bc6\u5e93\u4e2d",
        "\u6211\u7684\u77e5\u8bc6\u5e93\u91cc",
        "\u4ece\u77e5\u8bc6\u5e93",
        "\u5728\u77e5\u8bc6\u5e93",
        "\u77e5\u8bc6\u5e93\u4e2d",
        "\u77e5\u8bc6\u5e93\u91cc",
        "\u77e5\u8bc6\u5e93",
        "\u6587\u732e\u5e93",
        "\u8d44\u6599\u5e93",
        "personal library",
        "my library",
        "knowledge base",
        "literature library",
    ]
    return any(marker in q for marker in query_markers)


def looks_like_web_read_query(query: str) -> bool:
    q = query.lower()
    if _URL_RE.search(query or ""):
        return True
    search_markers = [
        "搜",
        "搜索",
        "搜寻",
        "查找",
        "找一下",
        "找找",
        "search",
        "find",
        "look up",
    ]
    code_markers = [
        "开源代码",
        "开源项目",
        "源代码",
        "源码",
        "代码仓库",
        "仓库",
        "github",
        "gitlab",
        "repo",
        "repository",
        "codebase",
        "open source",
        "open-source",
    ]
    if any(marker in q for marker in search_markers) and any(marker in q for marker in code_markers):
        return True
    markers = [
        "网页",
        "网站",
        "链接",
        "网址",
        "页面",
        "联网搜索",
        "网页搜索",
        "搜网页",
        "网上搜",
        "网上查",
        "互联网搜索",
        "web page",
        "website",
        "url",
        "link",
        "web search",
        "search the web",
        "online",
    ]
    return any(marker in q for marker in markers)


def should_use_cached_library_context(query: str, state: TaskState) -> bool:
    cached = state.working_memory.get("current_library_context") or {}
    if not cached.get("contexts"):
        return False
    if looks_like_explicit_library_search(query):
        return False

    q = query.lower()
    active_title = str(cached.get("active_title") or cached.get("title_filter") or "").strip()
    if active_title and active_title.lower() in q:
        return True
    if re.search(r"\.(pdf|pptx|txt|md|docx?)\b", q, flags=re.I):
        return False

    strong_followup_markers = [
        "\u8fd9\u7bc7",
        "\u8fd9\u7bc7\u6587\u7ae0",
        "\u8fd9\u7bc7\u8bba\u6587",
        "\u8be5\u8bba\u6587",
        "\u8be5\u6587\u7ae0",
        "\u8fd9\u7bc7paper",
        "\u8fd9\u7bc7\u6587\u732e",
        "\u8fd9\u7bc7\u5de5\u4f5c",
        "\u4e0a\u9762\u90a3\u7bc7",
        "\u521a\u624d\u90a3\u7bc7",
        "\u524d\u9762\u90a3\u7bc7",
        "\u4e0a\u4e00\u6761\u7ed3\u679c",
        "\u521a\u624d\u68c0\u7d22\u5230\u7684",
        "this paper",
        "this article",
        "that paper",
        "the previous paper",
        "previous result",
    ]
    return any(marker in q for marker in strong_followup_markers)


def is_contextual_library_followup(query: str, state: TaskState) -> bool:
    return should_use_cached_library_context(query, state)


def looks_like_explicit_library_search(query: str) -> bool:
    q = query.lower()
    markers = [
        "\u67e5\u8be2\u6211\u7684\u77e5\u8bc6\u5e93",
        "\u641c\u7d22\u6211\u7684\u77e5\u8bc6\u5e93",
        "\u68c0\u7d22\u6211\u7684\u77e5\u8bc6\u5e93",
        "\u6211\u7684\u77e5\u8bc6\u5e93\u91cc\u6709\u6ca1\u6709",
        "\u77e5\u8bc6\u5e93\u91cc\u6709\u6ca1\u6709",
        "\u77e5\u8bc6\u5e93\u4e2d\u662f\u5426\u5305\u542b",
        "\u77e5\u8bc6\u5e93\u662f\u5426\u6709",
        "\u662f\u5426\u6709\u8fd9\u65b9\u9762",
        "\u6709\u6ca1\u6709\u8fd9\u65b9\u9762",
        "\u8fd9\u65b9\u9762\u7684\u77e5\u8bc6",
        "\u4ece\u77e5\u8bc6\u5e93\u68c0\u7d22",
        "\u5728\u77e5\u8bc6\u5e93\u68c0\u7d22",
        "search my knowledge base",
        "search the knowledge base",
        "query my knowledge base",
        "in my knowledge base",
        "does my knowledge base",
        "knowledge base contain",
    ]
    return any(marker in q for marker in markers)


def detect_qa_source(user_query: str, state: TaskState) -> str:
    q = user_query.lower()
    has_uploads = has_uploaded_writing_docs(state) or bool(state.working_memory.get("stored_papers"))
    if looks_like_web_read_query(user_query):
        return "web"
    upload_markers = [
        "\u4e0a\u4f20",
        "\u8fd9\u4e2a\u6587\u4ef6",
        "\u8fd9\u4e2a pdf",
        "\u8fd9\u4e2apdf",
        "uploaded",
        "this file",
        "this pdf",
    ]
    if any(marker in q for marker in upload_markers):
        return "uploaded_file" if has_uploads else "library"
    if looks_like_library_query(user_query):
        return "library"
    if has_uploads:
        return "uploaded_file"
    return "library"


def _legacy_intent_to_workflow(intent: str) -> str:
    return {
        "literature_search": "paper_search_workflow",
        "paper_download": "paper_search_workflow",
        "research_literature_reading": "paper_search_workflow",
        "summarize_session": "conversation_summary_workflow",
        "paper_qa": "question_answer_workflow",
        "library_qa": "question_answer_workflow",
        "image_understanding": "image_understanding_workflow",
        "add_to_library": "library_ingest_workflow",
        "paper_writing": "academic_writing_workflow",
        "create_note": "note_workflow",
        "create_note_from_chat": "note_workflow",
        "create_note_from_summary": "note_workflow",
        "create_note_from_reading": "note_workflow",
        "update_note": "note_workflow",
        "delete_note": "note_workflow",
        "search_note": "note_workflow",
        "embed_note": "note_workflow",
        "reembed_note": "note_workflow",
        "list_notes": "note_workflow",
        "general_chat": "chat_workflow",
    }.get(intent, "")
