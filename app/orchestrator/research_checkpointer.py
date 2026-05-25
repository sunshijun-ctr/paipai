"""Postgres-backed checkpointer for ResearchAgent's 3-node graph.

Phase C — scaffolding only:

- One process-global ``AsyncPostgresSaver`` instance, lazily initialised
  on first use, backed by a small ``AsyncConnectionPool``.
- ``setup()`` is idempotent and creates the langgraph-checkpoint tables
  (``checkpoints``, ``checkpoint_blobs``, ``checkpoint_writes``,
  ``checkpoint_migrations``).
- ``build_research_agent_graph`` compiles the StateGraph with the saver,
  and the executor passes ``configurable.thread_id = task_id`` on every
  ``ainvoke`` so each task gets its own checkpoint thread.

The checkpointer writes one row per node boundary (3 writes per query),
which is cheap. Phase D will use this same saver to back ``interrupt()``
and the resume API — the tables and pool are already in place.

If ``DATABASE_URL`` is unset / unreachable, the module returns None and
the graph compiles without a checkpointer (existing behavior).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)


_saver = None                 # AsyncPostgresSaver | None
_pool = None                  # AsyncConnectionPool | None
_init_attempted = False       # set to True after first init attempt (success or fail)


async def init_research_checkpointer() -> Optional[object]:
    """Idempotent. Returns the saver on success, None when DB is not
    configured or initialization failed. Safe to call once per process
    on startup (called from FastAPI's lifespan)."""
    global _saver, _pool, _init_attempted

    if _init_attempted:
        return _saver
    _init_attempted = True

    url = (settings.database_url or "").strip()
    if not url:
        logger.info("research checkpointer: DATABASE_URL not set, skipping")
        return None

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        logger.warning("research checkpointer: langgraph-checkpoint-postgres not importable: %s", exc)
        return None

    # psycopg connection-string compat: langgraph-checkpoint expects
    # `postgresql://...` (or `postgres://...`); SQLAlchemy-style
    # `postgresql+psycopg://` would fail. Normalise.
    conn_str = url
    if "+psycopg" in conn_str:
        conn_str = conn_str.replace("postgresql+psycopg", "postgresql").replace("postgres+psycopg", "postgres")

    try:
        # autocommit=True is REQUIRED — the checkpointer's setup() runs
        # DDL that cannot be wrapped in a transaction with row locks.
        _pool = AsyncConnectionPool(
            conn_str,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await _pool.open()
        _saver = AsyncPostgresSaver(_pool)
        await _saver.setup()
        logger.info("research checkpointer: AsyncPostgresSaver initialised (pool max=4)")
    except Exception as exc:
        logger.warning("research checkpointer: init failed (%s) — graph will run without persistence", exc)
        if _pool is not None:
            try:
                await _pool.close()
            except Exception:
                pass
        _pool = None
        _saver = None

    return _saver


def get_research_checkpointer() -> Optional[object]:
    """Returns the saver if initialised, else None. Cheap, sync."""
    return _saver


async def close_research_checkpointer() -> None:
    """Called from FastAPI lifespan shutdown. Best-effort."""
    global _saver, _pool, _init_attempted
    _saver = None
    if _pool is not None:
        try:
            await _pool.close()
        except Exception as exc:
            logger.warning("research checkpointer: pool close failed: %s", exc)
    _pool = None
    _init_attempted = False
