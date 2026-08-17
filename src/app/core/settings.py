import os
from functools import cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.exceptions import DomainErrors


def _read_bool(  # pyright: ignore[reportUnusedFunction]
    name: str, default: bool
) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _read_port(default: int) -> int:  # pyright: ignore[reportUnusedFunction]
    port = int(os.getenv("PORT", str(default)))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    return port


def _read_mute_loggers() -> tuple[str, ...]:  # pyright: ignore[reportUnusedFunction]
    return tuple(
        value.strip()
        for value in os.getenv("MUTE_LOGGERS", "").split(",")
        if value.strip()
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

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
    DATABASE_HOST: str = "identity-postgres"
    DATABASE_PORT: int = Field(default=5432, ge=1, le=65535)
    DATABASE_USER: str = "andruha_identity"
    DATABASE_PASSWORD: str = "identity-local-only"
    DATABASE_NAME: str = "andruha_identity"
    RUN_MIGRATIONS: bool = False
    VALKEY_HOST: str = "valkey"
    VALKEY_PORT: int = Field(default=6379, ge=1, le=65535)
    VALKEY_DB: int = Field(default=0, ge=0)
    VALKEY_KEY_NAMESPACE: str = "andruha-identity-service:idempotency:v1"
    IDEMPOTENCY_LEASE_SECONDS: int = Field(default=30, gt=0)
    IDEMPOTENCY_RESULT_TTL_SECONDS: int = Field(default=300, gt=0)
    IDEMPOTENCY_CB_FAILURES: int = Field(default=3, gt=0)
    IDEMPOTENCY_CB_RECOVERY_SECONDS: float = Field(default=10.0, gt=0)
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    AUTH_TEST_TOKEN_ENDPOINT_ENABLED: bool = False
    JWT_PRIVATE_KEY_PATH: Path = Path("/run/secrets/identity_jwt_private_key")
    JWT_PUBLIC_KEY_PATH: Path = Path("/run/secrets/identity_jwt_public_key")
    JWT_ACTIVE_KEY_ID: str = "identity-v1"
    JWT_ISSUER: str = "andruha-identity-service"
    JWT_SERVICE_AUDIENCE: str = "andruha-identity-service"
    jwt_audiences_raw: str = Field(
        default="andruha-identity-service,andruha-api-gateway",
        validation_alias="JWT_AUDIENCES",
    )
    JWT_CLOCK_SKEW_SECONDS: int = Field(default=30, ge=0)
    ACCESS_TOKEN_TTL_SECONDS: int = Field(default=900, gt=0)
    AUTH_SESSION_IDLE_TTL_SECONDS: int = Field(default=2_592_000, gt=0)
    REPLAY_ENCRYPTION_ACTIVE_KEY_ID: str = "replay-v1"
    replay_key_paths_raw: str = Field(
        default="replay-v1=/run/secrets/identity_replay_key",
        validation_alias="REPLAY_ENCRYPTION_KEY_PATHS",
    )

    @property
    def DATABASE_URL(self) -> str:
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.DATABASE_USER,
                password=self.DATABASE_PASSWORD,
                host=self.DATABASE_HOST,
                port=self.DATABASE_PORT,
                path=self.DATABASE_NAME,
            )
        )

    @property
    def VALKEY_URL(self) -> str:
        return f"redis://{self.VALKEY_HOST}:{self.VALKEY_PORT}/{self.VALKEY_DB}?socket_connect_timeout=2&socket_timeout=2"

    @property
    def JWT_AUDIENCES(self) -> frozenset[str]:
        return frozenset(
            value.strip()
            for value in self.jwt_audiences_raw.split(",")
            if value.strip()
        )

    @property
    def REPLAY_ENCRYPTION_KEY_PATHS(self) -> dict[str, Path]:
        pairs: dict[str, Path] = {}
        for item in self.replay_key_paths_raw.split(","):
            key_id, separator, path = item.strip().partition("=")
            if not separator or not key_id or not path:
                raise ValueError(
                    "REPLAY_ENCRYPTION_KEY_PATHS must contain key_id=path entries"
                )
            pairs[key_id] = Path(path)
        return pairs

    @property
    def test_token_endpoint_enabled(self) -> bool:
        return (
            self.APP_ENVIRONMENT != "production"
            and self.AUTH_TEST_TOKEN_ENDPOINT_ENABLED
        )

    @field_validator(
        "SERVICE_NAME",
        "JWT_ACTIVE_KEY_ID",
        "JWT_ISSUER",
        "JWT_SERVICE_AUDIENCE",
        "REPLAY_ENCRYPTION_ACTIVE_KEY_ID",
        "VALKEY_KEY_NAMESPACE",
    )
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("security and identity settings must not be blank")
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

    @model_validator(mode="after")
    def validate_security_contract(self) -> Self:
        if self.JWT_SERVICE_AUDIENCE not in self.JWT_AUDIENCES:
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        if self.REPLAY_ENCRYPTION_ACTIVE_KEY_ID not in self.REPLAY_ENCRYPTION_KEY_PATHS:
            raise ValueError(
                "active replay encryption key must be present in the key ring"
            )
        if self.AUTH_COOKIE_SAMESITE == "none" and not self.AUTH_COOKIE_SECURE:
            raise ValueError("SameSite=None requires secure cookies")
        if (
            self.APP_ENVIRONMENT == "production"
            and self.AUTH_TEST_TOKEN_ENDPOINT_ENABLED
        ):
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        return self


@cache
def get_settings() -> Settings:
    return Settings()
