from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from app.core.settings import get_settings
from app.entrypoints.http.main import create_app
from tests.integration.profile_server import ProfilePeer, serve_profile_peer


@dataclass(frozen=True, slots=True)
class IdentityInfrastructure:
    database_url: str
    valkey_url: str
    jwt_private_key_path: Path
    jwt_public_key_path: Path
    replay_key_path: Path
    managed_by_testcontainers: bool
    environment: Mapping[str, str]


def _split_database_url(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError("IDENTITY_TEST_DATABASE_URL must include host and port")
    return {
        "DATABASE_HOST": parsed.hostname,
        "DATABASE_PORT": str(parsed.port),
        "DATABASE_USER": parsed.username or "andruha_identity",
        "DATABASE_PASSWORD": parsed.password or "identity-local-only",
        "DATABASE_NAME": parsed.path.lstrip("/") or "andruha_identity",
    }


def _split_valkey_url(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError("IDENTITY_TEST_VALKEY_URL must include host and port")
    return {
        "VALKEY_HOST": parsed.hostname,
        "VALKEY_PORT": str(parsed.port),
        "VALKEY_DB": parsed.path.lstrip("/") or "0",
    }


def _write_test_keys(directory: Path) -> tuple[Path, Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = directory / "jwt-private.pem"
    public_path = directory / "jwt-public.pem"
    replay_path = directory / "replay-v1.key"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    replay_path.write_bytes(os.urandom(32))
    return private_path, public_path, replay_path


def _set_environment(values: Mapping[str, str]) -> dict[str, str | None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    get_settings.cache_clear()
    return previous


def _restore_environment(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _upgrade_database() -> None:
    config = Config("alembic.ini")
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def identity_infrastructure(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[IdentityInfrastructure]:
    """Provide disposable real PostgreSQL and Valkey dependencies.

    CI may inject Docker service-container URLs. Otherwise Testcontainers owns
    both dependencies and removes them after the session.
    """

    keys = _write_test_keys(tmp_path_factory.mktemp("identity-keys"))
    external_database_url = os.getenv("IDENTITY_TEST_DATABASE_URL")
    external_valkey_url = os.getenv("IDENTITY_TEST_VALKEY_URL")
    with ExitStack() as stack:
        profile_url = stack.enter_context(serve_profile_peer(_profile_peer))
        managed = not (external_database_url and external_valkey_url)
        if managed:
            postgres = stack.enter_context(
                PostgresContainer(
                    "postgres:18-alpine",
                    username="andruha_identity",
                    password="identity-test-only",
                    dbname="andruha_identity_test",
                    driver="asyncpg",
                )
            )
            valkey = stack.enter_context(
                RedisContainer(image="valkey/valkey:8.1-alpine", port=6379)
            )
            database_url = postgres.get_connection_url()
            valkey_url = (
                f"redis://{valkey.get_container_host_ip()}:"
                f"{valkey.get_exposed_port(6379)}/0"
            )
        else:
            database_url = external_database_url
            valkey_url = external_valkey_url

        assert database_url is not None
        assert valkey_url is not None
        environment = {
            "PROFILE_SERVICE_URL": profile_url,
            "PROFILE_SERVICE_TOKEN": "integration-profile-token",
            "PROFILE_SERVICE_TIMEOUT_SECONDS": "1",
            "PROFILE_SERVICE_RETRY_DELAY_SECONDS": "0",
            **_split_database_url(database_url),
            **_split_valkey_url(valkey_url),
            "IDENTITY_TEST_DATABASE_URL": database_url,
            "IDENTITY_TEST_VALKEY_URL": valkey_url,
            "APP_ENVIRONMENT": "test",
            "AUTH_COOKIE_SECURE": "false",
            "AUTH_TEST_TOKEN_ENDPOINT_ENABLED": "true",
            "JWT_PRIVATE_KEY_PATH": str(keys[0]),
            "JWT_PUBLIC_KEY_PATH": str(keys[1]),
            "REPLAY_ENCRYPTION_KEY_PATHS": f"replay-v1={keys[2]}",
            "JWT_CLOCK_SKEW_SECONDS": "0",
            "VALKEY_KEY_NAMESPACE": "andruha-identity-integration:idempotency:v1",
            "IDEMPOTENCY_LEASE_SECONDS": "2",
            "IDEMPOTENCY_RESULT_TTL_SECONDS": "30",
        }
        previous = _set_environment(environment)
        try:
            _upgrade_database()
            settings = get_settings()
            infrastructure = IdentityInfrastructure(
                database_url=settings.postgres.DATABASE_URL,
                valkey_url=settings.valkey.VALKEY_URL,
                jwt_private_key_path=keys[0],
                jwt_public_key_path=keys[1],
                replay_key_path=keys[2],
                managed_by_testcontainers=managed,
                environment=environment,
            )
        finally:
            _restore_environment(previous)
        yield infrastructure


@pytest.fixture(autouse=True)
def identity_test_environment(
    identity_infrastructure: IdentityInfrastructure,
) -> Iterator[None]:
    """Expose integration settings only while an integration test is running."""

    previous = _set_environment(identity_infrastructure.environment)
    try:
        yield
    finally:
        _restore_environment(previous)


async def _truncate_state(infrastructure: IdentityInfrastructure) -> None:
    engine = create_async_engine(infrastructure.database_url)
    valkey = Redis.from_url(infrastructure.valkey_url, decode_responses=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE idempotency_records, refresh_tokens, "
                    "auth_sessions, users, outbox, registration_operations CASCADE"
                )
            )
        await valkey.flushdb()
    finally:
        await valkey.aclose()
        await engine.dispose()


@pytest.fixture
def clean_identity_state(
    identity_infrastructure: IdentityInfrastructure,
) -> Iterator[IdentityInfrastructure]:
    asyncio.run(_truncate_state(identity_infrastructure))
    try:
        yield identity_infrastructure
    finally:
        asyncio.run(_truncate_state(identity_infrastructure))


@pytest.fixture
def identity_client(
    clean_identity_state: IdentityInfrastructure,
) -> Iterator[TestClient]:
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="https://testserver") as client:
        yield client
    get_settings.cache_clear()


_profile_peer = ProfilePeer()


@pytest.fixture
def profile_peer() -> Iterator[ProfilePeer]:
    _profile_peer.requests.clear()
    try:
        yield _profile_peer
    finally:
        _profile_peer.status = 204
        _profile_peer.requests.clear()


@pytest.fixture
async def database_sessionmaker(
    clean_identity_state: IdentityInfrastructure,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(clean_identity_state.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@pytest.fixture
async def database_session(
    clean_identity_state: IdentityInfrastructure,
) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(clean_identity_state.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
async def valkey_client(
    clean_identity_state: IdentityInfrastructure,
) -> AsyncIterator[Redis]:
    client = Redis.from_url(clean_identity_state.valkey_url, decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()
