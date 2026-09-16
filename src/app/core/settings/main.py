from functools import cache
from typing import Any, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.application.exceptions.security import InvalidTokenConfigurationError
from app.core.settings.app import AppSettings
from app.core.settings.idempotency import IdempotencySettings
from app.core.settings.kafka import KafkaSettings
from app.core.settings.outbox import OutboxSettings
from app.core.settings.postgres import PostgresSettings
from app.core.settings.profile_service import ProfileServiceSettings
from app.core.settings.registration import RegistrationSettings
from app.core.settings.security import SecuritySettings
from app.core.settings.valkey import ValkeySettings


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    valkey: ValkeySettings = Field(default_factory=ValkeySettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    idempotency: IdempotencySettings = Field(default_factory=IdempotencySettings)
    profile_service: ProfileServiceSettings = Field(
        default_factory=ProfileServiceSettings
    )
    registration: RegistrationSettings = Field(default_factory=RegistrationSettings)

    @model_validator(mode="before")
    @classmethod
    def _route_flat_data(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        result = dict(data)
        for group, field in cls.model_fields.items():
            settings_type = field.annotation
            if not isinstance(settings_type, type) or not issubclass(
                settings_type, BaseSettings
            ):
                continue
            aliases = {}
            for name, nested_field in settings_type.model_fields.items():
                canonical = nested_field.validation_alias or name
                if isinstance(canonical, str):
                    aliases[name.lower()] = canonical
                    aliases[canonical.lower()] = canonical
            flat = {
                aliases[str(key).lower()]: value
                for key, value in data.items()
                if str(key).lower() in aliases
            }
            nested = result.get(group, {})
            if flat and isinstance(nested, dict):
                result[group] = {**nested, **flat}
        return result

    @property
    def test_token_endpoint_enabled(self) -> bool:
        return (
            self.app.APP_ENVIRONMENT != "production"
            and self.security.AUTH_TEST_TOKEN_ENDPOINT_ENABLED
        )

    @model_validator(mode="after")
    def validate_security_contract(self) -> Self:
        if (
            self.app.APP_ENVIRONMENT == "production"
            and self.security.AUTH_TEST_TOKEN_ENDPOINT_ENABLED
        ):
            raise InvalidTokenConfigurationError()
        maximum_profile_attempt_seconds = (
            2 * self.profile_service.PROFILE_SERVICE_TIMEOUT_SECONDS
            + self.profile_service.PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS
        )
        if (
            maximum_profile_attempt_seconds
            >= self.registration.REGISTRATION_CLAIM_LEASE_SECONDS
        ):
            raise ValueError(
                "REGISTRATION_CLAIM_LEASE_SECONDS must exceed the maximum "
                "bounded Profile attempt window"
            )
        return self


@cache
def get_settings() -> Settings:
    return Settings()
