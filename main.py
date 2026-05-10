import asyncio
import json
import logging
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from app.memory.manager import MemoryManager
from app.orchestrator.orchestrator import Orchestrator

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s: %(message)s")

# Legacy session state file — used only for migrating old sessions on first startup
_LEGACY_STATE_FILE = os.path.join(".", "data", ".session_state.json")
_SESSION_ID_FILE = os.path.join(".", "data", ".current_session_id")


def _load_or_create_session_id() -> str:
    """Return the persisted session_id, or create a new one."""
    try:
        with open(_SESSION_ID_FILE, encoding="utf-8") as f:
            sid = f.read().strip()
            if sid:
                return sid
    except FileNotFoundError:
        pass
    sid = f"session_{uuid.uuid4().hex[:8]}"
    _save_session_id(sid)
    return sid


def _save_session_id(session_id: str) -> None:
    try:
        os.makedirs(os.path.dirname(_SESSION_ID_FILE), exist_ok=True)
        with open(_SESSION_ID_FILE, "w", encoding="utf-8") as f:
            f.write(session_id)
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to save session id: %s", exc)


def _migrate_legacy_state(memory: MemoryManager) -> None:
    """One-time migration: pull stored_papers from the old .session_state.json."""
    try:
        with open(_LEGACY_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        papers = [
            p for p in data.get("session_papers", [])
            if os.path.exists(p.get("local_path", ""))
        ]
        found = data.get("session_found_papers", [])
        if papers and not memory.short_term.stored_papers:
            memory.short_term.stored_papers = papers
        if found and not memory.short_term.found_papers:
            memory.short_term.found_papers = found
        if papers or found:
            memory.save()
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.getLogger(__name__).debug("Legacy state migration: %s", exc)


def _collect_assistant_reply(state) -> str:
    # Library operations (add_to_library, clear_temp_rag)
    lib_out = state.agent_outputs.get("library_agent", {})
    if lib_out:
        return lib_out.get("result", {}).get("reply", "")

    chat_out = state.agent_outputs.get("chat_agent", {})
    if chat_out:
        return chat_out.get("result", {}).get("reply", "")

    writing_out = state.agent_outputs.get("writing_agent", {})
    if writing_out:
        result = writing_out.get("result", {})
        return result.get("content", "")

    parts: list[str] = []
    read_out = state.agent_outputs.get("reading_agent", {})
    if read_out:
        for note in read_out.get("result", {}).get("reading_notes", []):
            parts.append(f"Paper: {note['title']}\nAnswer: {note.get('answer', '')}")

    sum_out = state.agent_outputs.get("summary_agent", {})
    if sum_out:
        parts.append(sum_out.get("result", {}).get("final_report", ""))

    return "\n\n".join(p for p in parts if p)


def _print_state(state) -> None:
    intent_out = state.agent_outputs.get("intent_agent", {})
    if intent_out:
        r = intent_out.get("result", {})
        print(f"\n[Intent] {r.get('user_intent')}  ->  {r.get('execution_plan')}")

    # Library agent output (add_to_library / clear_temp_rag)
    lib_out = state.agent_outputs.get("library_agent", {})
    if lib_out:
        print(f"\n[Library]  {lib_out.get('result', {}).get('reply', '')}")
        return

    lit_out = state.agent_outputs.get("literature_agent", {})
    if lit_out:
        r = lit_out.get("result", {})
        arts = lit_out.get("artifacts", {})
        downloaded = arts.get("downloaded_pdfs", [])
        print(f"\n[Literature]  found={r.get('total_found', 0)}  selected={len(r.get('selected_papers', []))}  downloaded={len(downloaded)}")
        for p in downloaded:
            print(f"  [OK] {p['title'][:70]}")

    read_out = state.agent_outputs.get("reading_agent", {})
    if read_out:
        notes = read_out.get("result", {}).get("reading_notes", [])
        print(f"\n[Reading]  {len(notes)} paper(s)")
        for note in notes:
            print(f"\n  Paper : {note['title'][:70]}")
            print(f"  Q     : {note['question']}")
            print(f"  A     : {note.get('answer', '')[:600]}")

    sum_out = state.agent_outputs.get("summary_agent", {})
    if sum_out:
        report = sum_out.get("result", {}).get("final_report", "")
        report_path = sum_out.get("artifacts", {}).get("report_path", "")
        print(f"\n[Summary]  -> {report_path}")
        print("-" * 50)
        print(report[:1500])

    writing_out = state.agent_outputs.get("writing_agent", {})
    if writing_out:
        result = writing_out.get("result", {})
        print(f"\n[Writing]  {result.get('task_type', '')}")
        if result.get("title"):
            print(f"  Title : {result.get('title')}")
        print(result.get("content", "")[:1500])

    chat_out = state.agent_outputs.get("chat_agent", {})
    if chat_out:
        print(f"\n[Assistant]  {chat_out.get('result', {}).get('reply', '')}")

    if state.errors:
        print(f"\n[Errors]  {state.errors}")


async def main() -> None:
    orchestrator = Orchestrator()

    session_id = _load_or_create_session_id()
    memory = MemoryManager(session_id=session_id, llm=orchestrator.llm)
    _migrate_legacy_state(memory)

    # Validate stored_papers: drop entries whose PDF file no longer exists
    valid_papers = [
        p for p in memory.short_term.stored_papers
        if os.path.exists(p.get("local_path", ""))
    ]
    if len(valid_papers) != len(memory.short_term.stored_papers):
        memory.short_term.stored_papers = valid_papers

    if valid_papers:
        print(f"Resumed session {session_id} with {len(valid_papers)} downloaded paper(s):")
        for p in valid_papers:
            print(f"  - {p.get('title', 'Unknown')[:70]}")
        print()

    if not memory.long_term.is_empty():
        ctx = memory.long_term.to_context_string()
        print(f"[Long-term memory loaded]\n{ctx}\n")

    # Show personal library contents if any
    try:
        from app.rag.long_term.store import get_lt_rag_store
        lt = get_lt_rag_store()
        lib_titles = await lt.list_documents()
        if lib_titles:
            print(f"[Personal library]  {len(lib_titles)} paper(s) in your long-term library:")
            for t in lib_titles[:5]:
                print(f"  - {t[:70]}")
            if len(lib_titles) > 5:
                print(f"  ... and {len(lib_titles) - 5} more")
            print()
    except Exception:
        pass

    print("Research Assistant ready. Type your question (or 'exit' to quit).\n")
    print("Tip: '加入知识库' adds the current papers to your personal library.")
    print("     '查询我的知识库' searches your personal library.")
    print("     '清除临时文档' clears the session's temporary RAG.\n")

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not query:
            continue
        if query.lower() in {"exit", "quit", "bye", "q"}:
            print("Bye.")
            break

        print("Thinking...\n")
        state = await orchestrator.run(
            query,
            session_id=session_id,
            conversation_history=memory.short_term.get_full_history(),
            stored_papers=memory.short_term.stored_papers,
            found_papers=memory.short_term.found_papers,
            memory_manager=memory,
        )
        _print_state(state)

        # Update found/downloaded paper lists from literature agent output
        lit_out = state.agent_outputs.get("literature_agent", {})
        intent_out = state.agent_outputs.get("intent_agent", {})
        user_intent = intent_out.get("result", {}).get("user_intent", "")

        if lit_out:
            r = lit_out.get("result", {})

            if user_intent in {"literature_search", "research_literature_reading"}:
                new_found = r.get("selected_papers", [])
                if new_found:
                    memory.short_term.found_papers = new_found
                    print(f"\n[Papers found]  (say '下载第X篇' or '下载全部' to download)")
                    for i, p in enumerate(new_found, 1):
                        citations = p.get("citations", 0)
                        score = p.get("relevance_score")
                        cite_str = f"  [{citations:,} citations]" if citations else ""
                        score_str = f"  sim={score:.2f}" if score is not None else ""
                        print(f"  [{i}] {p.get('title', 'Unknown')[:65]}{cite_str}{score_str}")

            new_downloaded = lit_out.get("artifacts", {}).get("downloaded_pdfs", [])
            if new_downloaded:
                existing = memory.short_term.stored_papers
                for p in new_downloaded:
                    if p not in existing:
                        existing.append(p)
                memory.short_term.stored_papers = existing
                indexed = state.working_memory.get("indexed_titles", [])
                print(f"\n[Downloaded] {len(new_downloaded)} paper(s). Total in session: {len(existing)}.")
                if indexed:
                    print(f"[RAG Ready]  {len(indexed)} paper(s) indexed — ask your question directly.")
                print("Tip: say '加入知识库' to save these papers to your personal library.")

        # clear_temp_rag also clears the in-memory paper lists
        if user_intent == "clear_temp_rag":
            memory.short_term.stored_papers = []
            memory.short_term.found_papers = []

        # Update short-term memory with this turn
        assistant_reply = _collect_assistant_reply(state)
        if assistant_reply:
            memory.update_after_turn(query, assistant_reply)

        memory.save()
        print()


if __name__ == "__main__":
    asyncio.run(main())
