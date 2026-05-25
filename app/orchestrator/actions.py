"""Deterministic action handlers.

Some "workflows" are not really multi-step reasoning — they are plain verbs
(ingest these files into the library; create/update/delete a note). They used
to live in ``app/workflows`` as LangGraph StateGraphs, but a graph with no
branching reasoning is just overhead. They have been *demoted* to the direct
action handlers below, which the orchestrator calls instead of building and
running a graph.

This mirrors the pre-existing inline handlers (``_handle_clear_temp_rag``) and
keeps the LangGraph ``WORKFLOW_REGISTRY`` for things that genuinely need a graph
(planning, HITL, multi-agent sequences). These actions route under their own
names — ``library_ingest_action`` / ``note_action`` (see ``ACTION_NAMES`` in
app/schemas/workflow.py) — so nothing in the codebase still calls them
"workflows". The old ``*_workflow`` names are kept only as legacy aliases for
in-flight / persisted sessions.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.agents.base.agent import BaseAgent
from app.schemas.agent import AgentInput, AgentStatus
from app.state.task_state import TaskState
from app.tools.base import ToolRegistry
from app.workflows.base import BuildAgentInput, ProgressCallback

logger = logging.getLogger(__name__)

_SUPPORTED_INGEST_EXTS = {".pdf", ".pptx", ".txt", ".md", ".text", ".rst"}


# ── Note action ─────────────────────────────────────────────────────────────


async def run_note_action(
    task_state: TaskState,
    agents: dict[str, BaseAgent],
    build_agent_input: BuildAgentInput,
    progress: ProgressCallback,
) -> TaskState:
    """Run a single note operation directly through ``note_agent``.

    The old note_graph fanned out into six nodes (create/update/delete/query/
    embed/organize) that ALL pointed at the same ``note_agent`` — the actual
    operation is selected inside the agent from ``input_data['task_type']``
    (set by the orchestrator's ``_build_agent_input``), so the fan-out was
    cosmetic. One direct call replaces the whole graph.
    """
    agent = agents.get("note_agent")
    if agent is None:
        logger.warning("[%s] note_agent not available", task_state.task_id)
        task_state.add_error("note_agent not available")
        return task_state

    await progress("note_agent", "Processing note task...", 45)
    agent_input = build_agent_input("note_agent", task_state.user_goal, task_state)
    output = await agent.run(agent_input, task_state)
    task_state.record_agent_output("note_agent", output.model_dump())
    if output.status == AgentStatus.FAILED:
        task_state.add_error(f"note_agent failed: {output.errors}")
    task_state.update_stage("note_done", "note_agent")
    await progress("note_agent_done", "Note task complete.", 75)
    return task_state


# ── Library ingest action ───────────────────────────────────────────────────


async def run_library_ingest_action(
    task_state: TaskState,
    progress: ProgressCallback,
) -> TaskState:
    """Validate and index user/downloaded/local files into the knowledge base.

    Pure deterministic pipeline (no LLM): resolve targets → validate paths →
    index via the ``add_to_library`` tool. (Formerly the library_ingest graph.)
    """
    intent = task_state.agent_outputs.get("intent_agent", {}).get("result", {})
    user_goal = task_state.user_goal

    # 1. Resolve which files to ingest.
    stored = [
        *task_state.working_memory.get("stored_papers", []),
        *task_state.active_files,
        *task_state.working_memory.get("active_files", []),
    ]
    selected = _resolve_ingest_targets(
        stored,
        intent.get("target_indices", []),
        intent.get("target_keywords", ""),
    )
    selected.extend(_extract_local_file_targets(user_goal, intent))
    selected = _dedupe_targets(selected)
    task_state.working_memory["library_ingest_candidates"] = selected
    task_state.working_memory["library_ingest_source"] = "documents" if selected else "none"

    # 2. Validate paths + extensions.
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for paper in selected:
        local_path = str(paper.get("local_path") or "")
        ext = os.path.splitext(local_path)[1].lower()
        if local_path and os.path.exists(local_path) and ext in _SUPPORTED_INGEST_EXTS:
            valid.append(paper)
        else:
            invalid.append(paper)
    task_state.working_memory["library_ingest_valid"] = valid
    task_state.working_memory["library_ingest_invalid"] = invalid

    if not valid:
        reply = (
            "下载未成功或没有可加入的文件。你可以：1) 重试下载；2) 上传文件；"
            "3) 或尝试下载其他文章。完成后再说“加入知识库”。"
        )
        task_state.record_agent_output("library_agent", {"result": {"reply": reply}})
        task_state.current_stage = "library_ingest_no_files"
        return task_state

    # 3. Index into the knowledge base.
    add_tool = ToolRegistry.get("add_to_library")
    added: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    await progress("library_ingest", "Indexing documents into the knowledge base...", 45)
    for paper in valid:
        title = str(
            paper.get("title")
            or os.path.basename(str(paper.get("local_path") or ""))
            or "Untitled"
        )
        result = await add_tool.execute(
            local_path=paper.get("local_path", ""),
            title=title,
            lib_id=paper.get("lib_id", "lt_docs"),
            extra_meta=_paper_extra_meta(paper),
        )
        if result.success:
            added.append({"title": title, **(result.data or {})})
            logger.info(
                "[%s] Added '%s' to library (%s chunks)",
                task_state.task_id,
                title,
                result.data.get("chunks_indexed", 0),
            )
        else:
            failed.append({"title": title, "error": result.error})

    reply = _format_ingest_reply(added, failed, invalid)
    task_state.record_agent_output(
        "library_agent",
        {
            "result": {
                "reply": reply,
                "added": added,
                "failed": failed,
                "invalid": invalid,
                "ingest_status": "success" if added and not failed else "partial" if added else "failed",
            }
        },
    )
    task_state.current_stage = "library_ingest_done"
    await progress("library_ingest_done", "Knowledge-base indexing complete.", 75)
    return task_state


# ── Library ingest helpers (moved from library_ingest_graph.py) ──────────────


def _resolve_ingest_targets(
    stored_papers: list[dict[str, Any]],
    target_indices: list[int],
    target_keywords: str,
) -> list[dict[str, Any]]:
    if not stored_papers:
        return []
    if target_indices:
        selected = []
        for index in target_indices:
            if 1 <= index <= len(stored_papers):
                selected.append(stored_papers[index - 1])
        return selected
    if target_keywords:
        keyword = target_keywords.lower()
        return [p for p in stored_papers if keyword in str(p.get("title", "")).lower()]
    return stored_papers


def _extract_local_file_targets(user_query: str, intent: dict) -> list[dict[str, Any]]:
    paths: list[str] = []
    for key in ("local_paths", "file_paths", "source_paths"):
        raw = intent.get(key)
        if isinstance(raw, list):
            paths.extend(str(item) for item in raw)
        elif isinstance(raw, str):
            paths.extend(_split_path_list(raw))

    for marker in ("LOCAL_FILE_PATHS=", "FILE_PATHS=", "LIBRARY_FILE_PATHS="):
        if marker in user_query:
            tail = user_query.split(marker, 1)[1].splitlines()[0]
            paths.extend(_split_path_list(tail))

    paths.extend(_extract_quoted_paths(user_query))
    paths.extend(_extract_windows_paths(user_query))
    targets = []
    for path in paths:
        clean = path.strip().strip("\"'")
        if not clean:
            continue
        title = os.path.basename(clean) or clean
        targets.append({
            "title": title,
            "local_path": clean,
            "source": "local_path",
            "lib_id": intent.get("lib_id", "lt_docs") or "lt_docs",
        })
    return targets


def _split_path_list(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[|;\n]", raw) if item.strip()]


def _extract_quoted_paths(text: str) -> list[str]:
    candidates = re.findall(r'["“”](.+?\.(?:pdf|pptx|txt|md|text|rst))["“”]', text, flags=re.IGNORECASE)
    candidates += re.findall(r"[《](.+?\.(?:pdf|pptx|txt|md|text|rst))[》]", text, flags=re.IGNORECASE)
    return candidates


def _extract_windows_paths(text: str) -> list[str]:
    pattern = r"[A-Za-z]:\\[^\r\n\t\"'<>|]+?\.(?:pdf|pptx|txt|md|text|rst)"
    return re.findall(pattern, text, flags=re.IGNORECASE)


def _dedupe_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for target in targets:
        key = os.path.normcase(os.path.abspath(str(target.get("local_path") or target.get("title") or "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


def _paper_extra_meta(paper: dict[str, Any]) -> dict[str, Any]:
    keys = ("venue", "journal", "doi", "paper_id", "published_date", "authors", "citations", "source")
    meta = {key: paper.get(key) for key in keys if paper.get(key) not in (None, "")}
    if meta.get("source"):
        meta["paper_source"] = meta.pop("source")
    return meta


def _format_ingest_reply(
    added: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    invalid: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    if added:
        lines.append(f"已成功加入知识库：{len(added)} 篇")
        for item in added:
            chunks = item.get("chunks_indexed", 0)
            lines.append(f"- {item.get('title', 'Untitled')} ({chunks} chunks)")
    if failed:
        lines.append(f"加入失败：{len(failed)} 篇")
        for item in failed:
            lines.append(f"- {item.get('title', 'Untitled')}：{item.get('error', 'unknown error')}")
    if invalid:
        lines.append(f"文件不存在或不可读取：{len(invalid)} 篇")
        for item in invalid:
            lines.append(f"- {item.get('title', item.get('local_path', 'Untitled'))}")
    return "\n".join(lines) if lines else "没有处理任何文件。"
