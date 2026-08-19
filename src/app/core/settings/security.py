from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.core.settings.base import BaseContextSettings
from app.domain.exceptions import DomainErrors


class SecuritySettings(BaseContextSettings):
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    AUTH_TEST_TOKEN_ENDPOINT_ENABLED: bool = False
    JWT_PRIVATE_KEY_PATH: Path = Path("/run/secrets/identity_jwt_private_key")
    JWT_PUBLIC_KEY_PATH: Path = Path("/run/configs/identity_jwt_public_key")
    JWT_ACTIVE_KEY_ID: str = "identity-v1"
    JWT_ISSUER: str = "andruha-identity-service"
    JWT_SERVICE_AUDIENCE: str = "andruha-identity-service"
    jwt_audiences_raw: str = Field(
        default=(
            "andruha-identity-service,"
            "andruha-api-gateway,"
            "andruha-user-profile-service,"
            "andruha-messages-dialogues-service,"
            "andruha-websocket-gateway-service,"
            "andruha-object-storage-service"
        ),
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

    @field_validator("jwt_audiences_raw", mode="before")
    @classmethod
    def _normalize_audiences(cls, value: object) -> object:
        if isinstance(value, (list, tuple, set, frozenset)):
            return ",".join(str(item) for item in value)
        return value

    @field_validator("replay_key_paths_raw", mode="before")
    @classmethod
    def _normalize_replay_paths(cls, value: object) -> object:
        if isinstance(value, dict):
            return ",".join(f"{k}={v}" for k, v in value.items())
        return value

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

    @field_validator(
        "JWT_ACTIVE_KEY_ID",
        "JWT_ISSUER",
        "JWT_SERVICE_AUDIENCE",
        "REPLAY_ENCRYPTION_ACTIVE_KEY_ID",
    )
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("security and identity settings must not be blank")
        return value

    @model_validator(mode="after")
    def validate_security_invariants(self) -> Self:
        if self.JWT_SERVICE_AUDIENCE not in self.JWT_AUDIENCES:
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        if self.REPLAY_ENCRYPTION_ACTIVE_KEY_ID not in self.REPLAY_ENCRYPTION_KEY_PATHS:
            raise ValueError(
                "active replay encryption key must be present in the key ring"
            )
        if self.AUTH_COOKIE_SAMESITE == "none" and not self.AUTH_COOKIE_SECURE:
            raise ValueError("SameSite=None requires secure cookies")
        return self
