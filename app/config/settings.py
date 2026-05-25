from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM provider selection
    llm_provider: str = "ollama"

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # OpenAI
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: Optional[str] = None

    # Gemini (Google AI, OpenAI-compatible)
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    # Anthropic
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-6"

    # Qwen (Alibaba DashScope)
    qwen_api_key: Optional[str] = None
    qwen_model: str = "qwen-plus"

    # Doubao / Volcengine Ark (OpenAI-compatible)
    doubao_api_key: Optional[str] = None
    ark_api_key: Optional[str] = None
    doubao_model: str = "doubao-seed-1-6-251015"
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"

    # Per-agent LLM defaults. UI-saved data/memory/llm_config.json overrides these.
    intent_agent_llm_provider: Optional[str] = None
    intent_agent_llm_model: Optional[str] = None
    intent_agent_api_key: Optional[str] = None
    general_agent_llm_provider: Optional[str] = None
    general_agent_llm_model: Optional[str] = None
    general_agent_api_key: Optional[str] = None
    rag_agent_llm_provider: Optional[str] = None
    rag_agent_llm_model: Optional[str] = None
    rag_agent_api_key: Optional[str] = None
    web_agent_llm_provider: Optional[str] = None
    web_agent_llm_model: Optional[str] = None
    web_agent_api_key: Optional[str] = None
    writing_agent_llm_provider: Optional[str] = None
    writing_agent_llm_model: Optional[str] = None
    writing_agent_api_key: Optional[str] = None
    note_agent_llm_provider: Optional[str] = None
    note_agent_llm_model: Optional[str] = None
    note_agent_api_key: Optional[str] = None
    summary_agent_llm_provider: Optional[str] = None
    summary_agent_llm_model: Optional[str] = None
    summary_agent_api_key: Optional[str] = None
    evaluator_agent_llm_provider: Optional[str] = None
    evaluator_agent_llm_model: Optional[str] = None
    evaluator_agent_api_key: Optional[str] = None
    figure_prompt_llm_provider: Optional[str] = None
    figure_prompt_llm_model: Optional[str] = None
    figure_drawing_llm_provider: Optional[str] = None
    figure_drawing_llm_model: Optional[str] = None
    figure_image_provider: Optional[str] = None
    figure_image_model: Optional[str] = None
    figure_image_base_url: Optional[str] = None

    # Image understanding tool (OCR + vision-language model)
    vlm_provider: Optional[str] = None
    vlm_model_name: Optional[str] = None
    vlm_base_url: Optional[str] = None
    vlm_api_key: Optional[str] = None
    vlm_temperature: float = 0.2
    vlm_max_tokens: int = 2048
    qwen_vl_model: str = "qwen-vl-max"

    # RAG answer evaluation
    eval_enabled: bool = True
    eval_backend: str = "llm"     # custom | llm | ragas
    eval_mode: str = "sync"       # sync | off
    eval_min_contexts: int = 1
    eval_faithfulness_warning_threshold: float = 0.7
    eval_relevancy_warning_threshold: float = 0.65
    eval_precision_warning_threshold: float = 0.6

    # Search
    semantic_scholar_api_key: Optional[str] = None
    default_search_sources: str = "semantic"
    default_search_max_results: int = 20
    tavily_api_key: Optional[str] = None
    serper_api_key: Optional[str] = None

    # Web reading / scraping
    firecrawl_api_key: Optional[str] = None
    firecrawl_base_url: str = "https://api.firecrawl.dev"
    web_read_max_urls: int = 5
    web_read_max_chars: int = 50000

    # Download
    unpaywall_email: Optional[str] = None
    use_scihub: bool = True
    scihub_base_url: str = "https://sci-hub.se"

    # PDF parsing (LlamaParse preferred, PyMuPDF fallback)
    use_llama_parse: bool = True
    llama_cloud_api_key: Optional[str] = None
    llama_parse_result_type: str = "markdown"
    default_chunk_size: int = 2000
    default_chunk_overlap: int = 200

    # Storage
    use_mock_storage: bool = True
    redis_url: str = "redis://localhost:6379"
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    database_url: Optional[str] = None

    # Reranker: "flashrank" | "cross_encoder" | "none"
    reranker_type: str = "flashrank"
    reranker_model: str = "ms-marco-MiniLM-L-12-v2"

    # App
    app_name: str = "Research Assistant"
    debug: bool = False
    data_dir: str = "./data"

    # Auth — JWT + cookies
    auth_jwt_secret: str = "dev-secret-please-change-in-production"
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_ttl_minutes: int = 15
    auth_refresh_token_ttl_days: int = 7
    auth_cookie_domain: Optional[str] = None
    auth_cookie_secure: bool = False
    auth_public_base_url: str = "http://localhost:8000"

    # SMTP — email sending. Empty smtp_host → fall back to console output.
    smtp_host: Optional[str] = None
    smtp_port: int = 465
    smtp_use_tls: bool = True
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_address: str = "noreply@example.com"
    smtp_from_name: str = "Research Assistant"

    # QQ OAuth (PR-3) — distinct from QQ_BOT_* (which is the bot SDK)
    qq_oauth_app_id: Optional[str] = None
    qq_oauth_app_key: Optional[str] = None
    qq_oauth_redirect_uri: str = "http://localhost:8000/api/auth/qq/callback"

    # Admin bootstrap — on startup, ensure this account exists and is_admin=true.
    # Existing data without an owner is claimed by this account.
    auth_admin_email: Optional[str] = None
    auth_admin_initial_password: Optional[str] = None
    auth_admin_display_name: str = "Admin"

    # Per-user storage quota — 500MB default. Admin is exempt.
    auth_storage_limit_bytes: int = 500 * 1024 * 1024
    auth_max_upload_bytes: int = 100 * 1024 * 1024   # single-file cap, 100MB

    # Password-reset rate limits (per email)
    auth_reset_cooldown_seconds: int = 300   # min wait between two reset emails
    auth_reset_daily_limit: int = 2          # max reset emails per rolling 24h

    # ── ResearchAgent human-in-the-loop (Phase D) ──────────────────────
    # When enabled, plans with ≥ research_hitl_min_steps pause and emit a
    # plan-approval event to the WebSocket. The user can approve / modify
    # / cancel; if no action within research_hitl_timeout_secs the plan
    # is auto-approved and execution resumes.
    research_hitl_enabled: bool = False
    research_hitl_min_steps: int = 3
    research_hitl_timeout_secs: int = 60

    # ── Frontend error tracking (Phase-1 #1.4) ──────────────────────────
    # When SENTRY_DSN is set, the chat page boots the Sentry browser SDK
    # and reports uncaught JS errors / unhandled promise rejections.
    # Leaving it empty disables the SDK entirely (safe default for dev).
    # DSN is NOT a secret — Sentry treats it as a public project id.
    sentry_dsn: Optional[str] = None

    # Optional HTTP / HTTPS / SOCKS proxy for Tavily + Serper. Set this when
    # running inside mainland China without direct reachability to
    # api.tavily.com or google.serper.dev. The rest of the system (国产 LLM
    # 等) bypasses it. Example: http://127.0.0.1:7890  or  socks5://127.0.0.1:1080
    web_search_proxy: Optional[str] = None

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug(cls, value):
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production", "off"}:
                return False
            if normalized in {"dev", "development", "on"}:
                return True
        return value


settings = Settings()
