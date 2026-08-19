from pydantic import Field, field_validator

from app.core.settings.base import BaseContextSettings


class ValkeySettings(BaseContextSettings):
    VALKEY_HOST: str = "valkey"
    VALKEY_PORT: int = Field(default=6379, ge=1, le=65535)
    VALKEY_DB: int = Field(default=0, ge=0)
    VALKEY_KEY_NAMESPACE: str = "andruha-identity-service:idempotency:v1"
    VALKEY_MAX_CONNECTIONS: int = Field(default=50, ge=1)
    VALKEY_SOCKET_TIMEOUT: float = Field(default=2.0, ge=0.1)
    VALKEY_SOCKET_CONNECT_TIMEOUT: float = Field(default=2.0, ge=0.1)
    VALKEY_HEALTH_CHECK_INTERVAL: int = Field(default=30, ge=0)

    @property
    def VALKEY_URL(self) -> str:
        return (
            f"redis://{self.VALKEY_HOST}:{self.VALKEY_PORT}/{self.VALKEY_DB}"
            "?socket_connect_timeout=2&socket_timeout=2"
        )

    @property
    def url(self) -> str:
        return self.VALKEY_URL

    @field_validator("VALKEY_KEY_NAMESPACE")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("VALKEY_KEY_NAMESPACE must not be blank")
        return value
