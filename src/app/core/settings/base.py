import os
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _read_port(default: int) -> int:
    port = int(os.getenv("PORT", str(default)))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    return port


def _read_mute_loggers() -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.getenv("MUTE_LOGGERS", "").split(",")
        if value.strip()
    )


class BaseContextSettings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )
