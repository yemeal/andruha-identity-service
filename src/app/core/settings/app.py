from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import NoDecode

from app.core.settings.base import BaseContextSettings


class AppSettings(BaseContextSettings):
    SERVICE_NAME: str = "andruha-identity-service"
    APP_VERSION: str = "0.1.0"
    APP_ENVIRONMENT: Literal["development", "test", "production"] = "development"
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8001, ge=1, le=65535)
    DEV_LOGS: bool = True
    LOG_LEVEL: str = "INFO"
    MUTE_LOGGERS: Annotated[tuple[str, ...], NoDecode] = (
        "uvicorn.access",
        "sqlalchemy",
    )

    @field_validator("SERVICE_NAME")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("service name must not be blank")
        return value

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("MUTE_LOGGERS", mode="before")
    @classmethod
    def normalize_mute_loggers(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value
