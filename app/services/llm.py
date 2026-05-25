import json
import logging
import os
import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from pydantic import BaseModel

from app.config.settings import settings

logger = logging.getLogger(__name__)

_MONITOR_INIT_SQL = """
CREATE TABLE IF NOT EXISTS llm_token_usage (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent_name TEXT NOT NULL DEFAULT 'default',
    task_name TEXT,
    session_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    is_streaming BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'success',
    error_msg TEXT,
    cost_yuan NUMERIC(12, 6)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_token_usage (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_provider ON llm_token_usage (provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_token_usage (model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent ON llm_token_usage (agent_name, created_at DESC);
"""

_PRICE_TABLE = {
    "qwen-max": {"prompt": 0.04, "completion": 0.12},
    "qwen-plus": {"prompt": 0.008, "completion": 0.024},
    "qwen-turbo": {"prompt": 0.003, "completion": 0.006},
    "qwen-long": {"prompt": 0.0005, "completion": 0.002},
    "doubao-pro-32k": {"prompt": 0.008, "completion": 0.008},
    "doubao-pro-128k": {"prompt": 0.005, "completion": 0.009},
    "doubao-lite-32k": {"prompt": 0.003, "completion": 0.006},
    "gpt-4o": {"prompt": 0.0018, "completion": 0.0072},
    "gpt-4o-mini": {"prompt": 0.00011, "completion": 0.00044},
}


def _monitor_db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url or "user:password@localhost" in url:
        return ""
    return url


def _calc_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    price = _PRICE_TABLE.get(model)
    if not price:
        for key, value in _PRICE_TABLE.items():
            if model.startswith(key):
                price = value
                break
    if not price:
        return None
    return round(prompt_tokens / 1000 * price["prompt"] + completion_tokens / 1000 * price["completion"], 6)


def _usage_value(usage: Optional[dict[str, Any]], *keys: str) -> int:
    if not usage:
        return 0
    for key in keys:
        value = usage.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _save_usage_sync(record: dict[str, Any]) -> None:
    url = _monitor_db_url()
    if not url:
        return
    try:
        import psycopg
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(_MONITOR_INIT_SQL)
                cur.execute(
                    """
                    INSERT INTO llm_token_usage (
                        provider, model, model_version, agent_name, task_name, session_id,
                        prompt_tokens, completion_tokens, cache_read_tokens, total_tokens,
                        latency_ms, is_streaming, status, error_msg, cost_yuan
                    ) VALUES (
                        %(provider)s, %(model)s, %(model_version)s, %(agent_name)s, %(task_name)s, %(session_id)s,
                        %(prompt_tokens)s, %(completion_tokens)s, %(cache_read_tokens)s, %(total_tokens)s,
                        %(latency_ms)s, %(is_streaming)s, %(status)s, %(error_msg)s, %(cost_yuan)s
                    )
                    """,
                    record,
                )
    except Exception as exc:
        logger.debug("Failed to save token usage: %s", exc)


async def _record_usage(
    *,
    provider: str,
    model: str,
    agent_name: str,
    usage: Optional[dict[str, Any]],
    latency_ms: int,
    status: str = "success",
    error_msg: Optional[str] = None,
    task_name: Optional[str] = None,
    session_id: Optional[str] = None,
    is_streaming: bool = False,
) -> None:
    prompt_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
    cache_read_tokens = _usage_value(usage, "cache_read_tokens", "prompt_cache_hit_tokens", "cache_read_input_tokens")
    total_tokens = _usage_value(usage, "total_tokens") or prompt_tokens + completion_tokens + cache_read_tokens
    record = {
        "provider": provider,
        "model": model or "unknown",
        "model_version": None,
        "agent_name": agent_name or "default",
        "task_name": task_name,
        "session_id": session_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_read_tokens": cache_read_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
        "is_streaming": is_streaming,
        "status": status,
        "error_msg": error_msg,
        "cost_yuan": _calc_cost(model or "", prompt_tokens, completion_tokens),
    }
    await asyncio.to_thread(_save_usage_sync, record)


def _fire_usage(**kwargs: Any) -> None:
    if not _monitor_db_url():
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_record_usage(**kwargs))
    except RuntimeError:
        asyncio.run(_record_usage(**kwargs))

_AGENT_CONFIG_PATH = os.path.join(".", "data", "memory", "llm_config.json")
_AGENT_NAMES = [
    "intent_agent",
    "general_agent",
    "literature_agent",
    "rag_agent",
    "web_agent",
    "writing_agent",
    "note_agent",
    "summary_agent",
    "evaluator_agent",
]
_AGENT_ENV_PREFIXES = {
    "intent_agent": "INTENT_AGENT",
    "general_agent": "GENERAL_AGENT",
    "literature_agent": "LITERATURE_AGENT",
    "rag_agent": "RAG_AGENT",
    "web_agent": "WEB_AGENT",
    "writing_agent": "WRITING_AGENT",
    "note_agent": "NOTE_AGENT",
    "summary_agent": "SUMMARY_AGENT",
    "evaluator_agent": "EVALUATOR_AGENT",
}


class LLMMessage(BaseModel):
    role: str  # system | user | assistant
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    usage: Optional[dict[str, Any]] = None


class ToolCall(BaseModel):
    """One function-call request the LLM emitted in a tool-using turn."""
    id: str
    name: str
    arguments: dict[str, Any]


class ToolCallResponse(BaseModel):
    """LLM response in a tool-calling loop. Either contains a final text answer
    (tool_calls=[]) or a list of tool invocations to execute and feed back."""
    content: str
    tool_calls: list[ToolCall] = []
    finish_reason: str = "stop"
    model: str
    usage: Optional[dict[str, Any]] = None


class BaseLLMProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        pass

    async def complete_json(
        self,
        messages: list[LLMMessage],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = await self.complete(messages, system=system, **kwargs)
        content = response.content.strip()
        # Strip markdown code fences if present
        if "```json" in content:
            content = content.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in content:
            content = content.split("```", 1)[1].split("```", 1)[0]
        return json.loads(content.strip())

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolCallResponse:
        """One LLM turn with function-calling. *messages* is the full multi-turn
        history in OpenAI chat-completions format (so it can carry prior
        `tool_calls` / `tool` role messages). *tools* is the OpenAI tools schema.

        Returns either a final-answer (tool_calls=[]) or a tool-call request.
        Subclasses that don't support function-calling should leave this raising
        NotImplementedError — the calling agent should detect and fail clearly."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support function-calling. "
            "Use an OpenAI-compatible provider (Qwen / Doubao / OpenAI / Gemini) "
            "for ResearchAgent."
        )


class OllamaProvider(BaseLLMProvider):
    def __init__(self, base_url: str, model: str, agent_name: str = "default") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider_name = "ollama"
        self.agent_name = agent_name

    async def complete(
        self,
        messages: list[LLMMessage],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        all_messages: list[dict[str, str]] = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(m.model_dump() for m in messages)

        t0 = time.time()
        status = "success"
        error_msg = None
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json={"model": self.model, "messages": all_messages, "stream": False},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            _fire_usage(
                provider=self.provider_name,
                model=self.model,
                agent_name=self.agent_name,
                usage=None,
                latency_ms=int((time.time() - t0) * 1000),
                status=status,
                error_msg=error_msg,
            )
            raise

        usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        }
        _fire_usage(
            provider=self.provider_name,
            model=data.get("model") or self.model,
            agent_name=self.agent_name,
            usage=usage,
            latency_ms=int((time.time() - t0) * 1000),
            status=status,
        )

        return LLMResponse(
            content=data["message"]["content"],
            model=data["model"],
            usage=usage,
        )


class OpenAIProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
        provider_name: str = "openai",
        agent_name: str = "default",
    ) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.provider_name = provider_name
        self.agent_name = agent_name

    async def complete(
        self,
        messages: list[LLMMessage],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        all_messages: list[dict[str, str]] = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(m.model_dump() for m in messages)

        t0 = time.time()
        task_name = kwargs.pop("task_name", None)
        session_id = kwargs.pop("session_id", None)
        try:
            resp = await self.client.chat.completions.create(
                model=self.model, messages=all_messages, **kwargs  # type: ignore[arg-type]
            )
        except Exception as exc:
            if not self._requires_stream_fallback(exc):
                _fire_usage(
                    provider=self.provider_name,
                    model=self.model,
                    agent_name=self.agent_name,
                    usage=None,
                    latency_ms=int((time.time() - t0) * 1000),
                    status="error",
                    error_msg=str(exc),
                    task_name=task_name,
                    session_id=session_id,
                )
                raise
            result = await self._complete_streaming(all_messages, **kwargs)
            _fire_usage(
                provider=self.provider_name,
                model=result.model,
                agent_name=self.agent_name,
                usage=result.usage,
                latency_ms=int((time.time() - t0) * 1000),
                task_name=task_name,
                session_id=session_id,
                is_streaming=True,
            )
            return result
        usage = resp.usage.model_dump() if resp.usage else None
        _fire_usage(
            provider=self.provider_name,
            model=resp.model,
            agent_name=self.agent_name,
            usage=usage,
            latency_ms=int((time.time() - t0) * 1000),
            task_name=task_name,
            session_id=session_id,
        )
        return LLMResponse(
            content=resp.choices[0].message.content or "",
            model=resp.model,
            usage=usage,
        )

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolCallResponse:
        all_messages: list[dict[str, Any]] = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        t0 = time.time()
        task_name = kwargs.pop("task_name", None)
        session_id = kwargs.pop("session_id", None)
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=all_messages,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                **kwargs,
            )
        except Exception as exc:
            _fire_usage(
                provider=self.provider_name,
                model=self.model,
                agent_name=self.agent_name,
                usage=None,
                latency_ms=int((time.time() - t0) * 1000),
                status="error",
                error_msg=str(exc),
                task_name=task_name,
                session_id=session_id,
            )
            raise

        choice = resp.choices[0]
        msg = choice.message
        tool_calls: list[ToolCall] = []
        for raw in (msg.tool_calls or []):
            try:
                args = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw_arguments": raw.function.arguments}
            tool_calls.append(ToolCall(id=raw.id, name=raw.function.name, arguments=args))

        usage = resp.usage.model_dump() if resp.usage else None
        _fire_usage(
            provider=self.provider_name,
            model=resp.model,
            agent_name=self.agent_name,
            usage=usage,
            latency_ms=int((time.time() - t0) * 1000),
            task_name=task_name,
            session_id=session_id,
        )
        return ToolCallResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            model=resp.model,
            usage=usage,
        )

    @staticmethod
    def _requires_stream_fallback(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "does not support http call" in text
            or "does not support non-streaming" in text
            or "only support stream mode" in text
            or "please enable the stream parameter" in text
        )

    async def _complete_streaming(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> LLMResponse:
        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            **stream_kwargs,
        )

        parts: list[str] = []
        model = self.model
        async for chunk in stream:
            model = getattr(chunk, "model", None) or model
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                    else:
                        text = getattr(item, "text", None)
                    if text:
                        parts.append(str(text))

        return LLMResponse(content="".join(parts), model=model, usage=None)


class QwenProvider(OpenAIProvider):
    """Alibaba Qwen via DashScope OpenAI-compatible endpoint."""

    _BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(self, api_key: str, model: str, agent_name: str = "default") -> None:
        super().__init__(api_key=api_key, model=model, base_url=self._BASE_URL, provider_name="qwen", agent_name=agent_name)


class DoubaoProvider(OpenAIProvider):
    """ByteDance Doubao via Volcengine Ark OpenAI-compatible endpoint."""

    def __init__(self, api_key: str, model: str, base_url: str, agent_name: str = "default") -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, provider_name="doubao", agent_name=agent_name)


class GeminiProvider(OpenAIProvider):
    """Google Gemini via OpenAI-compatible endpoint."""

    def __init__(self, api_key: str, model: str, base_url: str, agent_name: str = "default") -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, provider_name="gemini", agent_name=agent_name)


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str, agent_name: str = "default") -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.provider_name = "anthropic"
        self.agent_name = agent_name

    async def complete(
        self,
        messages: list[LLMMessage],
        system: Optional[str] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        import anthropic

        t0 = time.time()
        task_name = kwargs.pop("task_name", None)
        session_id = kwargs.pop("session_id", None)
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=kwargs.get("max_tokens", 4096),
                system=system or anthropic.NOT_GIVEN,
                messages=[m.model_dump() for m in messages],  # type: ignore[arg-type]
            )
        except Exception as exc:
            _fire_usage(
                provider=self.provider_name,
                model=self.model,
                agent_name=self.agent_name,
                usage=None,
                latency_ms=int((time.time() - t0) * 1000),
                status="error",
                error_msg=str(exc),
                task_name=task_name,
                session_id=session_id,
            )
            raise
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        _fire_usage(
            provider=self.provider_name,
            model=resp.model,
            agent_name=self.agent_name,
            usage=usage,
            latency_ms=int((time.time() - t0) * 1000),
            task_name=task_name,
            session_id=session_id,
        )
        return LLMResponse(
            content=resp.content[0].text,
            model=resp.model,
            usage=usage,
        )


def _default_llm_config() -> dict[str, str]:
    return {
        "provider": settings.llm_provider,
        "model": {
            "ollama": settings.ollama_model,
            "openai": settings.openai_model,
            "anthropic": settings.anthropic_model,
            "qwen": settings.qwen_model,
            "doubao": settings.doubao_model,
            "gemini": settings.gemini_model,
        }.get(settings.llm_provider, settings.ollama_model),
    }


def _agent_env_llm_config(agent_name: str, default: dict[str, str]) -> dict[str, str]:
    prefix = _AGENT_ENV_PREFIXES[agent_name].lower()
    provider = getattr(settings, f"{prefix}_llm_provider", None) or default["provider"]
    model = getattr(settings, f"{prefix}_llm_model", None) or default["model"]
    return {"provider": provider, "model": model}


def _agent_env_api_key(agent_name: str) -> str:
    prefix = _AGENT_ENV_PREFIXES[agent_name].lower()
    return (getattr(settings, f"{prefix}_api_key", None) or "").strip()


def load_agent_llm_config() -> dict[str, dict[str, str]]:
    default = _default_llm_config()
    config = {name: _agent_env_llm_config(name, default) for name in _AGENT_NAMES}
    try:
        with open(_AGENT_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        for name in _AGENT_NAMES:
            if name not in raw:
                continue
            item = raw.get(name, {})
            provider = item.get("provider") or config[name]["provider"] or default["provider"]
            model = item.get("model") or config[name]["model"] or default["model"]
            config[name] = {"provider": provider, "model": model}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Failed to load agent LLM config: %s", exc)
    return config


def save_agent_llm_config(config: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    clean: dict[str, dict[str, str]] = {}
    default = _default_llm_config()
    valid_providers = {"ollama", "openai", "anthropic", "qwen", "doubao", "gemini"}
    for name in _AGENT_NAMES:
        item = config.get(name, {})
        provider = (item.get("provider") or default["provider"]).strip()
        model = (item.get("model") or default["model"]).strip()
        if provider not in valid_providers:
            raise ValueError(f"Invalid provider for {name}: {provider}")
        if not model:
            raise ValueError(f"Model is required for {name}")
        clean[name] = {"provider": provider, "model": model}

    os.makedirs(os.path.dirname(_AGENT_CONFIG_PATH), exist_ok=True)
    with open(_AGENT_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def get_available_llm_options() -> dict[str, Any]:
    return {
        "providers": ["ollama", "openai", "anthropic", "qwen", "doubao", "gemini"],
        "defaults": {
            "ollama": settings.ollama_model,
            "openai": settings.openai_model,
            "anthropic": settings.anthropic_model,
            "qwen": settings.qwen_model,
            "doubao": settings.doubao_model,
            "gemini": settings.gemini_model,
        },
        "agents": _AGENT_NAMES,
        "env_vars": {
            name: {
                "provider": f"{_AGENT_ENV_PREFIXES[name]}_LLM_PROVIDER",
                "model": f"{_AGENT_ENV_PREFIXES[name]}_LLM_MODEL",
                "api_key": f"{_AGENT_ENV_PREFIXES[name]}_API_KEY",
            }
            for name in _AGENT_NAMES
        },
    }


def get_llm_provider(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    agent_name: str = "default",
) -> BaseLLMProvider:
    provider = provider or settings.llm_provider
    if provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=model or settings.ollama_model,
            agent_name=agent_name,
        )
    if provider == "openai":
        return OpenAIProvider(
            api_key=(api_key or settings.openai_api_key or "").strip(),
            model=(model or settings.openai_model).strip(),
            base_url=settings.openai_base_url.strip() if settings.openai_base_url else None,
            agent_name=agent_name,
        )
    if provider == "anthropic":
        return AnthropicProvider(
            api_key=(api_key or settings.anthropic_api_key or "").strip(),
            model=(model or settings.anthropic_model).strip(),
            agent_name=agent_name,
        )
    if provider == "qwen":
        return QwenProvider(
            api_key=(api_key or settings.qwen_api_key or "").strip(),
            model=(model or settings.qwen_model).strip(),
            agent_name=agent_name,
        )
    if provider == "doubao":
        return DoubaoProvider(
            api_key=(api_key or settings.doubao_api_key or settings.ark_api_key or "").strip(),
            model=(model or settings.doubao_model).strip(),
            base_url=settings.doubao_base_url.strip(),
            agent_name=agent_name,
        )
    if provider == "gemini":
        return GeminiProvider(
            api_key=(api_key or settings.gemini_api_key or "").strip(),
            model=(model or settings.gemini_model).strip(),
            base_url=settings.gemini_base_url.strip(),
            agent_name=agent_name,
        )
    raise ValueError(f"Unknown LLM provider: '{provider}'. Choose from: ollama, openai, anthropic, qwen, doubao, gemini")


def get_agent_llm_provider(
    agent_name: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> BaseLLMProvider:
    """Create an LLM for one agent.

    Agent-specific API keys are optional. When absent, the provider-level
    global key is used, keeping simple deployments to a single key.
    """
    agent_key = _agent_env_api_key(agent_name)
    return get_llm_provider(provider, model, api_key=agent_key or None, agent_name=agent_name)


def get_agent_llm_providers() -> dict[str, BaseLLMProvider]:
    config = load_agent_llm_config()
    return {
        name: get_agent_llm_provider(name, item["provider"], item["model"])
        for name, item in config.items()
    }
