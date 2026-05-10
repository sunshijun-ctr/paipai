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
    literature_agent_llm_provider: Optional[str] = None
    literature_agent_llm_model: Optional[str] = None
    literature_agent_api_key: Optional[str] = None
    reading_agent_llm_provider: Optional[str] = None
    reading_agent_llm_model: Optional[str] = None
    reading_agent_api_key: Optional[str] = None
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
    chat_agent_llm_provider: Optional[str] = None
    chat_agent_llm_model: Optional[str] = None
    chat_agent_api_key: Optional[str] = None
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
