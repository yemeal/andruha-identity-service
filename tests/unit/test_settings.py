from pathlib import Path

import pytest

from app.core.settings import (
    AppSettings,
    IdempotencySettings,
    KafkaSettings,
    PostgresSettings,
    SecuritySettings,
    Settings,
    ValkeySettings,
    _read_bool,
    _read_mute_loggers,
    _read_port,
    get_settings,
)
from app.domain.exceptions import DomainErrors


@pytest.mark.parametrize("raw_value", ["1", "true", "TRUE", " yes ", "on"])
def test_read_bool_accepts_true_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    monkeypatch.setenv("FEATURE", raw_value)

    assert _read_bool("FEATURE", False) is True


@pytest.mark.parametrize("raw_value", ["0", "false", "FALSE", " no ", "off"])
def test_read_bool_accepts_false_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    monkeypatch.setenv("FEATURE", raw_value)

    assert _read_bool("FEATURE", True) is False


def test_read_bool_uses_default_when_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEATURE", raising=False)

    assert _read_bool("FEATURE", True) is True


def test_read_bool_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE", "sometimes")

    with pytest.raises(ValueError, match="FEATURE must be a boolean value"):
        _read_bool("FEATURE", False)


@pytest.mark.parametrize("port", [1, 8001, 65535])
def test_read_port_accepts_valid_range(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
) -> None:
    monkeypatch.setenv("PORT", str(port))

    assert _read_port(9000) == port


@pytest.mark.parametrize("port", [0, 65536])
def test_read_port_rejects_out_of_range_value(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
) -> None:
    monkeypatch.setenv("PORT", str(port))

    with pytest.raises(ValueError, match="PORT must be between 1 and 65535"):
        _read_port(9000)


def test_read_port_rejects_non_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORT", "http")

    with pytest.raises(ValueError):
        _read_port(9000)


def test_read_mute_loggers_trims_and_drops_empty_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUTE_LOGGERS", " uvicorn.access, ,httpx ")

    assert _read_mute_loggers() == ("uvicorn.access", "httpx")


def test_app_settings_defaults() -> None:
    settings = AppSettings()
    assert settings.SERVICE_NAME == "andruha-identity-service"
    assert settings.APP_VERSION == "0.1.0"
    assert settings.APP_ENVIRONMENT == "development"
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8001
    assert settings.DEV_LOGS is True
    assert settings.LOG_LEVEL == "INFO"
    assert "uvicorn.access" in settings.MUTE_LOGGERS


def test_app_settings_validation() -> None:
    with pytest.raises(ValueError, match="service name must not be blank"):
        AppSettings(SERVICE_NAME="   ")

    settings = AppSettings(LOG_LEVEL="debug", MUTE_LOGGERS="foo, bar")
    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.MUTE_LOGGERS == ("foo", "bar")


def test_postgres_settings_defaults_and_url() -> None:
    settings = PostgresSettings()
    assert settings.DATABASE_HOST == "identity-postgres"
    assert settings.DATABASE_PORT == 5432
    assert settings.DATABASE_USER == "andruha_identity"
    assert settings.DATABASE_NAME == "andruha_identity"
    assert settings.RUN_MIGRATIONS is False
    assert (
        settings.DATABASE_URL
        == "postgresql+asyncpg://andruha_identity:identity-local-only@identity-postgres:5432/andruha_identity"
    )
    assert settings.url == settings.DATABASE_URL


def test_valkey_settings_defaults_and_url() -> None:
    settings = ValkeySettings()
    assert settings.VALKEY_HOST == "valkey"
    assert settings.VALKEY_PORT == 6379
    assert settings.VALKEY_DB == 0
    assert settings.VALKEY_KEY_NAMESPACE == "andruha-identity-service:idempotency:v1"
    assert (
        settings.VALKEY_URL
        == "redis://valkey:6379/0?socket_connect_timeout=2&socket_timeout=2"
    )
    assert settings.url == settings.VALKEY_URL


def test_valkey_settings_blank_namespace_rejected() -> None:
    with pytest.raises(ValueError, match="VALKEY_KEY_NAMESPACE must not be blank"):
        ValkeySettings(VALKEY_KEY_NAMESPACE="   ")


def test_kafka_settings_defaults() -> None:
    settings = KafkaSettings()
    assert settings.KAFKA_BOOTSTRAP_SERVERS == "kafka:29092"
    assert settings.bootstrap_servers == "kafka:29092"
    assert settings.KAFKA_CLIENT_ID == "andruha-identity-service"
    assert settings.KAFKA_USER_REGISTERED_TOPIC == "identity.user-registered.v1"
    assert settings.KAFKA_GROUP_ID == "andruha-identity-service"
    assert settings.KAFKA_AUTO_OFFSET_RESET == "earliest"


def test_kafka_settings_blank_servers_rejected() -> None:
    with pytest.raises(
        ValueError, match="Kafka configuration fields must not be blank"
    ):
        KafkaSettings(KAFKA_BOOTSTRAP_SERVERS="   ")


def test_security_settings_defaults_and_properties() -> None:
    settings = SecuritySettings()
    assert settings.AUTH_COOKIE_SECURE is True
    assert settings.AUTH_COOKIE_SAMESITE == "lax"
    assert settings.AUTH_TEST_TOKEN_ENDPOINT_ENABLED is False
    assert settings.JWT_ACTIVE_KEY_ID == "identity-v1"
    assert settings.JWT_ISSUER == "andruha-identity-service"
    assert settings.JWT_SERVICE_AUDIENCE == "andruha-identity-service"
    expected_audiences = frozenset(
        [
            "andruha-identity-service",
            "andruha-api-gateway",
            "andruha-user-profile-service",
            "andruha-messages-dialogues-service",
            "andruha-websocket-gateway-service",
            "andruha-object-storage-service",
        ]
    )
    expected_replay_paths = {"replay-v1": Path("/run/secrets/identity_replay_key")}
    assert expected_audiences == settings.JWT_AUDIENCES
    assert expected_replay_paths == settings.REPLAY_ENCRYPTION_KEY_PATHS


def test_security_settings_validation_rules() -> None:
    # SameSite=None requires secure cookies
    with pytest.raises(ValueError, match="SameSite=None requires secure cookies"):
        SecuritySettings(AUTH_COOKIE_SAMESITE="none", AUTH_COOKIE_SECURE=False)

    # Missing active key in replay key paths
    with pytest.raises(
        ValueError,
        match="active replay encryption key must be present in the key ring",
    ):
        SecuritySettings(
            REPLAY_ENCRYPTION_ACTIVE_KEY_ID="missing-v2",
            replay_key_paths_raw="replay-v1=/path/to/key",
        )

    # Invalid key path format
    with pytest.raises(
        ValueError, match="REPLAY_ENCRYPTION_KEY_PATHS must contain key_id=path entries"
    ):
        _ = SecuritySettings(
            replay_key_paths_raw="invalid-format-without-equals",
        ).REPLAY_ENCRYPTION_KEY_PATHS

    # Service audience not in audiences
    with pytest.raises(DomainErrors.Token.INVALID_CONFIGURATION):
        SecuritySettings(
            JWT_SERVICE_AUDIENCE="untrusted-service",
            jwt_audiences_raw="andruha-identity-service,andruha-api-gateway",
        )


def test_idempotency_settings_defaults() -> None:
    settings = IdempotencySettings()
    assert settings.IDEMPOTENCY_LEASE_SECONDS == 30
    assert settings.IDEMPOTENCY_RESULT_TTL_SECONDS == 300
    assert settings.IDEMPOTENCY_CB_FAILURES == 3
    assert settings.IDEMPOTENCY_CB_RECOVERY_SECONDS == 10.0


def test_composite_settings_context_access() -> None:
    settings = Settings()
    assert isinstance(settings.app, AppSettings)
    assert isinstance(settings.postgres, PostgresSettings)
    assert isinstance(settings.valkey, ValkeySettings)
    assert isinstance(settings.kafka, KafkaSettings)
    assert isinstance(settings.security, SecuritySettings)
    assert isinstance(settings.idempotency, IdempotencySettings)

    assert settings.app.SERVICE_NAME == "andruha-identity-service"
    assert "postgresql+asyncpg" in settings.postgres.DATABASE_URL
    assert "redis://" in settings.valkey.VALKEY_URL
    assert settings.kafka.KAFKA_BOOTSTRAP_SERVERS == "kafka:29092"
    assert settings.security.JWT_ISSUER == "andruha-identity-service"
    assert settings.idempotency.IDEMPOTENCY_LEASE_SECONDS == 30


def test_composite_settings_cross_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_TEST_TOKEN_ENDPOINT_ENABLED", "true")

    with pytest.raises(DomainErrors.Token.INVALID_CONFIGURATION):
        Settings(
            app=AppSettings(APP_ENVIRONMENT="production"),
            security=SecuritySettings(AUTH_TEST_TOKEN_ENDPOINT_ENABLED=True),
        )


def test_get_settings_reads_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    values = {
        "SERVICE_NAME": "test-service",
        "APP_VERSION": "9.9.9",
        "APP_ENVIRONMENT": "test",
        "HOST": "127.0.0.1",
        "PORT": "9123",
        "DEV_LOGS": "false",
        "LOG_LEVEL": "debug",
        "MUTE_LOGGERS": "httpx,uvicorn.access",
        "DATABASE_HOST": "custom-pg",
        "DATABASE_PORT": "5433",
        "VALKEY_HOST": "custom-valkey",
        "VALKEY_PORT": "6380",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka-cluster:9092",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = get_settings()

    assert settings.app.SERVICE_NAME == "test-service"
    assert settings.app.APP_VERSION == "9.9.9"
    assert settings.app.APP_ENVIRONMENT == "test"
    assert settings.app.HOST == "127.0.0.1"
    assert settings.app.PORT == 9123
    assert settings.app.DEV_LOGS is False
    assert settings.app.LOG_LEVEL == "DEBUG"
    assert settings.app.MUTE_LOGGERS == ("httpx", "uvicorn.access")
    assert settings.postgres.DATABASE_HOST == "custom-pg"
    assert settings.postgres.DATABASE_PORT == 5433
    assert settings.valkey.VALKEY_HOST == "custom-valkey"
    assert settings.valkey.VALKEY_PORT == 6380
    assert settings.kafka.KAFKA_BOOTSTRAP_SERVERS == "kafka-cluster:9092"


@pytest.mark.asyncio
async def test_di_container_resolves_all_settings_contexts() -> None:
    from app.core.settings import get_settings
    from app.infrastructure.di import create_container

    expected_settings = get_settings()
    container = create_container()
    try:
        assert await container.get(Settings) == expected_settings
        assert await container.get(AppSettings) == expected_settings.app
        assert await container.get(PostgresSettings) == expected_settings.postgres
        assert await container.get(ValkeySettings) == expected_settings.valkey
        assert await container.get(KafkaSettings) == expected_settings.kafka
        assert await container.get(SecuritySettings) == expected_settings.security
        assert await container.get(IdempotencySettings) == expected_settings.idempotency
    finally:
        await container.close()
