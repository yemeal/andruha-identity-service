"""Environment-backed bootstrap settings with no external dependency."""

from dataclasses import dataclass
from functools import lru_cache
import os


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
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
    raw_value = os.getenv("MUTE_LOGGERS", "")
    return tuple(
        logger_name.strip()
        for logger_name in raw_value.split(",")
        if logger_name.strip()
    )


@dataclass(frozen=True, slots=True)
class Settings:
    SERVICE_NAME: str
    APP_VERSION: str
    APP_ENVIRONMENT: str
    HOST: str
    PORT: int
    DEV_LOGS: bool
    LOG_LEVEL: str
    MUTE_LOGGERS: tuple[str, ...]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        SERVICE_NAME=os.getenv("SERVICE_NAME", "andruha-identity-service"),
        APP_VERSION=os.getenv("APP_VERSION", "0.1.0"),
        APP_ENVIRONMENT=os.getenv("APP_ENVIRONMENT", "development"),
        HOST=os.getenv("HOST", "0.0.0.0"),
        PORT=_read_port(8001),
        DEV_LOGS=_read_bool("DEV_LOGS", True),
        LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO").upper(),
        MUTE_LOGGERS=_read_mute_loggers(),
    )
