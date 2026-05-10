from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class QQConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_app_id: Optional[str] = Field(default=None, alias="QQ_BOT_APP_ID")
    bot_token: Optional[str] = Field(default=None, alias="QQ_BOT_TOKEN")
    bot_secret: Optional[str] = Field(default=None, alias="QQ_BOT_SECRET")
    sandbox: bool = Field(default=True, alias="QQ_BOT_SANDBOX")
    api_base_url: str = Field(
        default="https://api.sgroup.qq.com",
        alias="QQ_API_BASE_URL",
    )
    sandbox_api_base_url: str = Field(
        default="https://sandbox.api.sgroup.qq.com",
        alias="QQ_SANDBOX_API_BASE_URL",
    )
    event_mode: Literal["websocket", "webhook"] = Field(
        default="websocket",
        alias="QQ_EVENT_MODE",
    )
    reply_timeout_seconds: float = Field(default=50.0, alias="QQ_REPLY_TIMEOUT_SECONDS")
    max_reply_length: int = Field(default=1800, alias="QQ_MAX_REPLY_LENGTH")
    enable_message_dedup: bool = Field(default=True, alias="QQ_ENABLE_MESSAGE_DEDUP")
    enable_user_whitelist: bool = Field(default=False, alias="QQ_ENABLE_USER_WHITELIST")
    allowed_users: str = Field(default="", alias="QQ_ALLOWED_USERS")
    allowed_groups: str = Field(default="", alias="QQ_ALLOWED_GROUPS")

    @property
    def resolved_api_base_url(self) -> str:
        return self.sandbox_api_base_url if self.sandbox else self.api_base_url

    @property
    def allowed_user_set(self) -> set[str]:
        return {item.strip() for item in self.allowed_users.split(",") if item.strip()}

    @property
    def allowed_group_set(self) -> set[str]:
        return {item.strip() for item in self.allowed_groups.split(",") if item.strip()}

    @model_validator(mode="after")
    def validate_reply_limits(self) -> "QQConfig":
        if self.max_reply_length < 200:
            raise ValueError("QQ_MAX_REPLY_LENGTH must be at least 200")
        if self.reply_timeout_seconds <= 0:
            raise ValueError("QQ_REPLY_TIMEOUT_SECONDS must be positive")
        return self

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in {
                "QQ_BOT_APP_ID": self.bot_app_id,
                "QQ_BOT_SECRET": self.bot_secret,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Missing QQ bot configuration: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_qq_config() -> QQConfig:
    return QQConfig()
