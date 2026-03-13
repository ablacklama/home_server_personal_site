from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    debug: bool

    database_url: str

    admin_token: str | None

    notify_enabled: bool
    ntfy_base_url: str
    ntfy_topic: str
    ntfy_token: str | None
    ntfy_user: str | None
    ntfy_password: str | None

    inactivity_notify_enabled: bool
    inactivity_seconds: int
    inactivity_cooldown_seconds: int

    ai_enabled: bool
    openai_api_key: str | None
    openai_model: str
    ai_debug_log: bool


def get_settings() -> Settings:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    debug = _env_bool("DEBUG", False)

    database_url = os.getenv(
        "DATABASE_URL", "sqlite:///./data/personal_site.sqlite3"
    ).strip()

    admin_token = os.getenv("ADMIN_TOKEN")

    notify_enabled = _env_bool("NOTIFY_ENABLED", False)
    ntfy_base_url = os.getenv("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
    ntfy_topic = os.getenv("NTFY_TOPIC", "")
    ntfy_token = os.getenv("NTFY_TOKEN")
    ntfy_user = os.getenv("NTFY_USER")
    ntfy_password = os.getenv("NTFY_PASSWORD")

    inactivity_notify_enabled = _env_bool("INACTIVITY_NOTIFY_ENABLED", False)
    inactivity_seconds = int(os.getenv("INACTIVITY_SECONDS", "3600"))
    inactivity_cooldown_seconds = int(os.getenv("INACTIVITY_COOLDOWN_SECONDS", "21600"))

    ai_enabled = _env_bool("AI_ENABLED", False)
    openai_api_key = os.getenv("OPENAI_API_KEY")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-5.2").strip() or "gpt-5.2"
    ai_debug_log = _env_bool("AI_DEBUG_LOG", False)

    return Settings(
        host=host,
        port=port,
        debug=debug,
        database_url=database_url,
        admin_token=admin_token,
        notify_enabled=notify_enabled,
        ntfy_base_url=ntfy_base_url,
        ntfy_topic=ntfy_topic,
        ntfy_token=ntfy_token,
        ntfy_user=ntfy_user,
        ntfy_password=ntfy_password,
        inactivity_notify_enabled=inactivity_notify_enabled,
        inactivity_seconds=inactivity_seconds,
        inactivity_cooldown_seconds=inactivity_cooldown_seconds,
        ai_enabled=ai_enabled,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        ai_debug_log=ai_debug_log,
    )
