from functools import cache
from typing import Any, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.settings.app import AppSettings
from app.core.settings.idempotency import IdempotencySettings
from app.core.settings.kafka import KafkaSettings
from app.core.settings.outbox import OutboxSettings
from app.core.settings.postgres import PostgresSettings
from app.core.settings.security import SecuritySettings
from app.core.settings.valkey import ValkeySettings
from app.domain.exceptions import DomainErrors


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

    @model_validator(mode="before")
    @classmethod
    def _route_flat_data(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        app_keys = {
            "service_name",
            "app_version",
            "app_environment",
            "host",
            "port",
            "dev_logs",
            "log_level",
            "mute_loggers",
        }
        postgres_keys = {
            "database_host",
            "database_port",
            "database_user",
            "database_password",
            "database_name",
            "run_migrations",
            "database_pool_size",
            "database_max_overflow",
            "database_pool_timeout",
            "database_pool_recycle",
        }
        valkey_keys = {
            "valkey_host",
            "valkey_port",
            "valkey_db",
            "valkey_key_namespace",
            "valkey_max_connections",
            "valkey_socket_timeout",
            "valkey_socket_connect_timeout",
            "valkey_health_check_interval",
        }
        kafka_keys = {
            "kafka_bootstrap_servers",
            "kafka_client_id",
            "kafka_user_registered_topic",
            "kafka_group_id",
            "kafka_auto_offset_reset",
        }
        security_keys = {
            "auth_cookie_secure",
            "auth_cookie_samesite",
            "auth_test_token_endpoint_enabled",
            "jwt_private_key_path",
            "jwt_public_key_path",
            "jwt_active_key_id",
            "jwt_issuer",
            "jwt_service_audience",
            "jwt_audiences_raw",
            "jwt_audiences",
            "jwt_clock_skew_seconds",
            "access_token_ttl_seconds",
            "auth_session_idle_ttl_seconds",
            "replay_encryption_active_key_id",
            "replay_key_paths_raw",
            "replay_encryption_key_paths",
        }
        idempotency_keys = {
            "idempotency_lease_seconds",
            "idempotency_result_ttl_seconds",
            "idempotency_cb_failures",
            "idempotency_cb_recovery_seconds",
        }
        outbox_keys = {
            "outbox_poll_interval_seconds",
            "outbox_batch_size",
            "outbox_claim_lease_seconds",
            "outbox_retry_initial_seconds",
            "outbox_retry_max_seconds",
            "outbox_retry_exponent",
            "outbox_retry_jitter_ratio",
            "outbox_retry_max_attempts",
            "outbox_shutdown_timeout_seconds",
        }

        app_data: dict[str, Any] = {}
        postgres_data: dict[str, Any] = {}
        valkey_data: dict[str, Any] = {}
        kafka_data: dict[str, Any] = {}
        outbox_data: dict[str, Any] = {}
        security_data: dict[str, Any] = {}
        idempotency_data: dict[str, Any] = {}

        for k, v in list(data.items()):
            k_lower = k.lower()
            if k_lower in app_keys:
                app_data[k] = v
            elif k_lower in postgres_keys:
                postgres_data[k] = v
            elif k_lower in valkey_keys:
                valkey_data[k] = v
            elif k_lower in kafka_keys:
                kafka_data[k] = v
            elif k_lower in outbox_keys:
                outbox_data[k] = v
            elif k_lower in security_keys:
                security_data[k] = v
            elif k_lower in idempotency_keys:
                idempotency_data[k] = v

        result = dict(data)
        if app_data:
            if "app" in result and isinstance(result["app"], dict):
                result["app"] = {**result["app"], **app_data}
            elif "app" not in result:
                result["app"] = app_data

        if postgres_data:
            if "postgres" in result and isinstance(result["postgres"], dict):
                result["postgres"] = {**result["postgres"], **postgres_data}
            elif "postgres" not in result:
                result["postgres"] = postgres_data

        if valkey_data:
            if "valkey" in result and isinstance(result["valkey"], dict):
                result["valkey"] = {**result["valkey"], **valkey_data}
            elif "valkey" not in result:
                result["valkey"] = valkey_data

        if kafka_data:
            if "kafka" in result and isinstance(result["kafka"], dict):
                result["kafka"] = {**result["kafka"], **kafka_data}
            elif "kafka" not in result:
                result["kafka"] = kafka_data

        if outbox_data:
            if "outbox" in result and isinstance(result["outbox"], dict):
                result["outbox"] = {**result["outbox"], **outbox_data}
            elif "outbox" not in result:
                result["outbox"] = outbox_data

        if security_data:
            if "security" in result and isinstance(result["security"], dict):
                result["security"] = {**result["security"], **security_data}
            elif "security" not in result:
                result["security"] = security_data

        if idempotency_data:
            if "idempotency" in result and isinstance(result["idempotency"], dict):
                result["idempotency"] = {**result["idempotency"], **idempotency_data}
            elif "idempotency" not in result:
                result["idempotency"] = idempotency_data

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
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        return self


@cache
def get_settings() -> Settings:
    return Settings()
