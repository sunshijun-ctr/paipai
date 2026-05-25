"""In-process resume-signal registry for ResearchAgent HITL checkpoints.

When ``plan_node`` calls ``langgraph.types.interrupt(...)``, the graph
pauses. ``run_agent_workflow`` then waits on an ``asyncio.Event`` keyed
by ``task_id``. The HTTP endpoint ``POST /api/research/{task_id}/resume``
calls :func:`signal_resume` which stores the user's decision and fires
the event — :func:`wait_for_resume` returns, then the executor calls
``graph.ainvoke(Command(resume=decision), config={thread_id})``.

If the timeout elapses with no signal, :func:`wait_for_resume` returns
the default ``{"action": "approve"}`` so the plan runs as-is.

Process-scoped — multi-worker deployments would need Redis or DB-backed
signaling instead. Single-uvicorn-worker is the current shape.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class _Pending:
    __slots__ = ("event", "decision", "owner_user_id")

    def __init__(self, owner_user_id: str = "") -> None:
        self.event: asyncio.Event = asyncio.Event()
        self.decision: Optional[dict[str, Any]] = None
        # Whoever started the workflow. The resume endpoint compares
        # this against the authenticated user before accepting the
        # decision; without this any logged-in user could cancel /
        # modify someone else's task by guessing the task_id.
        self.owner_user_id: str = owner_user_id


# task_id → pending checkpoint
_pending: dict[str, _Pending] = {}


def has_pending(task_id: str) -> bool:
    """True when a checkpoint is awaiting user decision for this task."""
    return task_id in _pending


def get_owner(task_id: str) -> Optional[str]:
    """Return the user_id that owns the pending checkpoint, or None
    if there's no active checkpoint for this task. Used by the resume
    endpoint to gate access."""
    pending = _pending.get(task_id)
    return pending.owner_user_id if pending is not None else None


def open_checkpoint(task_id: str, owner_user_id: str = "") -> _Pending:
    """Register a new pending checkpoint. Called by the executor right
    before it waits, so the resume endpoint can find it. *owner_user_id*
    is the authenticated user that started the workflow — required for
    the ownership check in the resume API."""
    if task_id in _pending:
        # Should not happen — task_ids are unique per chat turn — but be
        # defensive: overwrite stale state.
        logger.warning("HITL: overwriting an already-pending checkpoint for %s", task_id)
    pending = _Pending(owner_user_id=owner_user_id)
    _pending[task_id] = pending
    return pending


async def wait_for_resume(task_id: str, timeout_secs: float) -> dict[str, Any]:
    """Block until :func:`signal_resume` fires or the timeout elapses.
    Returns the resume decision dict. Default is ``{"action": "approve"}``
    when the timer wins — keeps the workflow moving when no UI listens."""
    pending = _pending.get(task_id)
    if pending is None:
        # Race: signal might have arrived before the executor registered.
        # Treat as auto-approve.
        return {"action": "approve"}
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=timeout_secs)
        decision = pending.decision or {"action": "approve"}
        logger.info("HITL: %s resumed with decision=%s", task_id, decision.get("action"))
        return decision
    except asyncio.TimeoutError:
        logger.info("HITL: %s timed out after %.0fs — auto-approving", task_id, timeout_secs)
        return {"action": "approve"}
    finally:
        _pending.pop(task_id, None)


def signal_resume(task_id: str, decision: dict[str, Any]) -> bool:
    """Called by the resume API endpoint when the user clicks a button.
    Returns True when a checkpoint was waiting, False otherwise."""
    pending = _pending.get(task_id)
    if pending is None:
        return False
    pending.decision = decision
    pending.event.set()
    return True


def cancel_pending(task_id: str) -> None:
    """Drop a pending checkpoint without signalling. Used when the chat
    request gets cancelled before the user responds."""
    pending = _pending.pop(task_id, None)
    if pending is not None and not pending.event.is_set():
        # Wake the waiter so it doesn't hang on a dead event.
        pending.decision = {"action": "cancel"}
        pending.event.set()
