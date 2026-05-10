"""MemoryManager — coordinates long-term, short-term, and working memory.

Write mechanism (per design doc):
- Agents generate memory_candidates (list[dict]) in their output
- MemoryManager.process_candidates() is the sole writer to long-term memory
- Short-term memory is updated after every conversation turn
- Long-term memory is written after SummaryAgent runs

RAG integration:
- Conclusions are also indexed into lt_memory (LongTermRAGStore) so that
  build_agent_context() can do a *semantic* search instead of dumping
  every conclusion into every prompt.
- User documents (lt_docs) are managed separately via AddToLibraryTool;
  MemoryManager never touches lt_docs.
"""
import logging
from typing import Optional, TYPE_CHECKING

from app.memory.long_term.store import LongTermMemoryStore
from app.memory.short_term.store import ShortTermMemoryStore
from app.session.context import SessionContext

if TYPE_CHECKING:
    from app.services.llm import BaseLLMProvider

logger = logging.getLogger(__name__)

# Long-term memory JSON store is shared across all sessions (singleton)
_long_term_store: Optional[LongTermMemoryStore] = None
_session_contexts: dict[str, SessionContext] = {}


def _get_long_term() -> LongTermMemoryStore:
    global _long_term_store
    if _long_term_store is None:
        _long_term_store = LongTermMemoryStore()
    return _long_term_store


class MemoryManager:
    def __init__(self, session_id: str, llm: Optional["BaseLLMProvider"] = None) -> None:
        self._session_id = session_id
        self._llm = llm
        self.long_term: LongTermMemoryStore = _get_long_term()
        self.short_term: ShortTermMemoryStore = ShortTermMemoryStore(session_id)

    def set_llm(self, llm: Optional["BaseLLMProvider"]) -> None:
        """Update the LLM used for background memory tasks such as compression."""
        self._llm = llm

    def load_session_context(self) -> SessionContext:
        """Load this session's structured context from the short-term store."""
        ctx = self.short_term.session_context
        _session_contexts[self._session_id] = ctx
        return ctx

    def save_session_context(self, ctx: SessionContext) -> None:
        """Persist the structured context for this session."""
        _session_contexts[self._session_id] = ctx
        self.short_term.session_context = ctx

    # ── Context building ──────────────────────────────────────────────────────

    async def build_agent_context(self, query: str = "") -> str:
        """Return combined memory context string for injection into agent prompts.

        When *query* is provided, the long-term memory section is built by
        semantically searching lt_memory for relevant past conclusions rather
        than dumping everything.  Falls back to the full JSON-based context
        if lt_memory is empty or the search fails.
        """
        parts: list[str] = []

        # ── Long-term: prefer semantic search in lt_memory ────────────────────
        lt_context = ""
        profile_context = self.long_term.profile_to_context_string()
        if query:
            try:
                from app.rag.long_term.store import get_lt_rag_store
                lt = get_lt_rag_store()
                hits = await lt.search_memory(query, k=5)
                if hits:
                    lines = [f"- {h['document']}" for h in hits]
                    lt_context = "Relevant past conclusions:\n" + "\n".join(lines)
                    if profile_context:
                        lt_context = profile_context + "\n" + lt_context
            except Exception as exc:
                logger.debug("lt_memory semantic search failed, using JSON fallback: %s", exc)

        if not lt_context:
            lt_context = self.long_term.to_context_string()

        if lt_context:
            parts.append(f"[Long-term memory]\n{lt_context}")

        # ── Short-term ────────────────────────────────────────────────────────
        st = self.short_term.to_context_string()
        if st:
            parts.append(f"[Short-term memory]\n{st}")

        return "\n\n".join(parts)

    # ── Short-term updates ────────────────────────────────────────────────────

    def update_after_turn(self, user_msg: str, assistant_msg: str) -> None:
        """Called after each conversation turn to append to short-term memory."""
        if not user_msg or not assistant_msg:
            return
        self.short_term.add_turn(user_msg, assistant_msg)
        if self.short_term.needs_compression():
            self._compress_history()

    def update_focus(self, focus: str) -> None:
        if focus:
            self.short_term.current_focus = focus

    async def compress_now(self) -> None:
        """Manually compress older turns when the user requests it."""
        await self._async_compress(force=True)

    def infer_focus_from_state(self, state) -> str:
        """Extract current_focus from agent outputs in TaskState."""
        read_out = state.agent_outputs.get("reading_agent", {})
        if read_out:
            notes = read_out.get("result", {}).get("reading_notes", [])
            if notes:
                titles = [n.get("title", "") for n in notes if n.get("title")]
                if titles:
                    return f"Reading: {' | '.join(t[:50] for t in titles[:2])}"

        lit_out = state.agent_outputs.get("literature_agent", {})
        if lit_out:
            selected = lit_out.get("result", {}).get("selected_papers", [])
            if selected:
                return f"Searching: {selected[0].get('title', '')[:60]}"

        intent_out = state.agent_outputs.get("intent_agent", {})
        if intent_out:
            task_type = intent_out.get("result", {}).get("task_type", "")
            if task_type:
                return task_type

        return ""

    # ── Long-term memory write (unified, via candidates) ──────────────────────

    async def process_candidates(self, candidates: list[dict]) -> None:
        """Process memory candidates from agents and write to long-term memory.

        Also indexes any 'conclusion' candidates into lt_memory (separate from
        lt_docs) so they can be retrieved semantically later.

        Candidate format:
          {"type": "profile",     "data": {"background": "...", ...}}
          {"type": "preference",  "data": {"output_style": "...", ...}}
          {"type": "conclusion",  "content": "...", "topic": "..."}
        """
        if not candidates:
            return

        written = 0
        conclusions_to_index: list[tuple[str, str]] = []

        for c in candidates:
            kind = c.get("type", "")
            if kind == "profile":
                data = c.get("data", {})
                if data:
                    self.long_term.update_profile(data)
                    written += 1
            elif kind == "preference":
                data = c.get("data", {})
                if data:
                    self.long_term.update_preferences(data)
                    written += 1
            elif kind == "conclusion":
                content = c.get("content", "").strip()
                topic = c.get("topic", "general")
                if content:
                    self.long_term.add_conclusion(content, topic, self._session_id)
                    conclusions_to_index.append((content, topic))
                    written += 1
            else:
                logger.debug("Unknown memory candidate type: %s", kind)

        if written:
            self.long_term.save()
            logger.info("MemoryManager: wrote %d candidate(s) to long-term memory", written)

        # Index conclusions into lt_memory for semantic retrieval
        if conclusions_to_index:
            try:
                from app.rag.long_term.store import get_lt_rag_store
                lt = get_lt_rag_store()
                for content, topic in conclusions_to_index:
                    await lt.index_conclusion(content, topic, self._session_id)
                logger.info(
                    "MemoryManager: indexed %d conclusion(s) into lt_memory",
                    len(conclusions_to_index),
                )
            except Exception as exc:
                logger.warning("lt_memory indexing failed (non-fatal): %s", exc)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        self.short_term.save()
        # long_term JSON is saved only when candidates are processed

    # ── Internal ──────────────────────────────────────────────────────────────

    def _compress_history(self) -> None:
        """Compress old turns into history_summary. Uses LLM if available."""
        if not self._llm:
            self.short_term.compress_old_turns()
            return

        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # Async context (FastAPI/uvicorn): fire-and-forget, never block the event loop
            loop.create_task(self._async_compress())
        except RuntimeError:
            # No running loop — synchronous CLI context
            try:
                asyncio.run(self._async_compress())
            except Exception as exc:
                logger.debug("LLM compression error: %s", exc)
                self.short_term.compress_old_turns()

    async def _async_compress(self, force: bool = False) -> None:
        from app.services.llm import LLMMessage
        turns = self.short_term.recent_turns
        overflow_count = len(turns) - 100
        if force:
            overflow_count = max(len(turns) - 20, 0)
        if overflow_count <= 0:
            return
        overflow = turns[:overflow_count]
        text = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in overflow)
        try:
            resp = await self._llm.complete(
                messages=[LLMMessage(role="user", content=(
                    "Summarize the following conversation turns. Output only valid JSON "
                    "with this exact shape:\n"
                    "{\n"
                    '  "summary": "...",\n'
                    '  "current_task": "..."\n'
                    "}\n\n"
                    "Requirements:\n"
                    "- summary: summarize the historical conversation.\n"
                    "- current_task: one sentence describing the user's current follow-up "
                    "or unfinished concrete intent.\n"
                    "- Keep current_task separate; do not fold it into summary.\n"
                    "- Do not include markdown fences or any text outside the JSON.\n\n"
                    f"Conversation turns:\n\n{text}"
                ))],
                system="You are a concise memory compression assistant. Output only valid JSON.",
            )
            self.short_term.compress_old_turns(llm_summary=resp.content)
        except Exception as exc:
            logger.debug("LLM compression call failed: %s", exc)
            self.short_term.compress_old_turns()
