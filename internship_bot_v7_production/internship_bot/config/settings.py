from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ANTHROPIC_API_KEY: str = ""
    DATABASE_URL: str = "postgresql+asyncpg://user:pass@localhost:5432/internship_bot"
    REDIS_URL: str = "redis://localhost:6379/0"
    DRY_RUN: bool = True

    # Email monitoring
    IMAP_HOST: str = "imap.gmail.com"
    IMAP_PORT: int = 993
    IMAP_USER: str = ""
    IMAP_PASS: str = ""

    # SMTP notifications
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    NOTIFY_EMAIL_TO: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Vault
    VAULT_MASTER_PASSWORD: str = ""

    # Portal credentials
    DAAD_EMAIL: str = ""
    DAAD_PASSWORD: str = ""
    EURAXESS_EMAIL: str = ""
    EURAXESS_PASSWORD: str = ""
    HANDSHAKE_EMAIL: str = ""
    HANDSHAKE_PASSWORD: str = ""

    # Proxies
    PROXY_LIST: str = ""

    # Concurrency
    MAX_CONCURRENT_APPS: int = 5
    PLAYWRIGHT_WORKERS: int = 3
    ANALYTICS_EVERY_N: int = 10
    TRACKER_INTERVAL_HRS: int = 6

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_RELOAD: bool = False

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "console"


@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
