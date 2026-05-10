import logging
import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.channels.qq.qq_event_receiver import QQEventReceiver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels/qq", tags=["QQ Channel"])
_receiver: QQEventReceiver | None = None
_DEBUG_LOG_PATH = os.path.join(".", "data", "qq_webhook_events.jsonl")


def set_qq_event_receiver(receiver: QQEventReceiver | None) -> None:
    global _receiver
    _receiver = receiver


@router.post("/webhook")
async def qq_webhook(request: Request):
    if _receiver is None:
        raise HTTPException(503, "QQ channel is not configured")
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        _write_debug_event(request, {"raw_body": raw_body.decode("utf-8", "replace")}, "bad_json")
        raise HTTPException(400, f"invalid json: {exc}")
    _write_debug_event(request, payload, "received")
    try:
        result = await _receiver.handle_webhook(payload)
        _write_debug_event(request, {"result": result}, "handled")
        return result
    except ValueError as exc:
        _write_debug_event(request, {"error": str(exc)}, "value_error")
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("QQ webhook failed")
        _write_debug_event(request, {"error": str(exc)}, "exception")
        raise HTTPException(500, str(exc))


@router.get("/webhook")
async def qq_webhook_probe():
    return {"ok": True, "message": "QQ webhook accepts POST requests"}


@router.get("/debug/recent")
async def qq_debug_recent(limit: int = 20):
    limit = max(1, min(limit, 100))
    if not os.path.exists(_DEBUG_LOG_PATH):
        return {"events": []}
    with open(_DEBUG_LOG_PATH, encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return {"events": events}


def _write_debug_event(request: Request, payload: dict[str, Any], stage: str) -> None:
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "client": request.client.host if request.client else "",
            "method": request.method,
            "path": str(request.url.path),
            "headers": _safe_headers(request),
            "payload_summary": _payload_summary(payload),
        }
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to write QQ webhook debug event")


def _safe_headers(request: Request) -> dict[str, str]:
    keep = {
        "user-agent",
        "content-type",
        "x-bot-appid",
        "x-signature-method",
        "x-signature-timestamp",
    }
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() in keep
    }


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("op", "t", "type", "id", "s"):
        if key in payload:
            result[key] = payload.get(key)
    data = payload.get("d")
    if isinstance(data, dict):
        result["d_keys"] = sorted(data.keys())
        for key in (
            "id",
            "content",
            "text",
            "group_openid",
            "group_id",
            "guild_id",
            "channel_id",
            "event_ts",
            "plain_token",
        ):
            if key in data:
                value = str(data.get(key) or "")
                result[f"d.{key}"] = value[:200]
        author = data.get("author")
        if isinstance(author, dict):
            result["d.author_keys"] = sorted(author.keys())
    elif "result" in payload:
        result["result"] = payload.get("result")
    elif "error" in payload:
        result["error"] = payload.get("error")
    elif "raw_body" in payload:
        result["raw_body"] = str(payload.get("raw_body") or "")[:500]
    return result
