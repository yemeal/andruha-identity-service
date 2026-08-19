from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dishka import AsyncContainer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.ports.events import EventPublisherProtocol, UserRegisteredEvent
from app.application.ports.events.publisher import BrokerPublisherProtocol
from app.application.ports.idempotency import (
    DurableExecutionProtocol,
    HotIdempotencyStoreProtocol,
    IdempotencyCoordinatorProtocol,
    IdempotencyObserverProtocol,
    IdempotencyRecordRepositoryProtocol,
    ReplayResultProtectorProtocol,
)
from app.application.ports.outbox.scope_factory import OutboxScopeFactory
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    OutboxRepositoryProtocol,
    RefreshTokenRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    AccessTokenVerifierProtocol,
    OpaqueRefreshTokenCodecProtocol,
    PasswordHasherProtocol,
)
from app.application.ports.uow import AsyncUOWProtocol
from app.application.services.auth_service import AuthService, AuthServiceProtocol
from app.application.services.durable_idempotency import DurableExecutionService
from app.application.services.idempotency_coordinator import IdempotencyCoordinator
from app.application.services.outbox_relay import OutboxRelayService
from app.application.services.refresh import (
    RefreshUseCase,
    RefreshUseCaseProtocol,
    TransactionalRefreshOperation,
    TransactionalRefreshOperationProtocol,
)
from app.core.settings import (
    AppSettings,
    IdempotencySettings,
    KafkaSettings,
    OutboxSettings,
    PostgresSettings,
    SecuritySettings,
    Settings,
    ValkeySettings,
    get_settings,
)
from app.infrastructure.database.repositories.auth_session_repository import (
    AuthSessionRepository,
)
from app.infrastructure.database.repositories.idempotency_record_repository import (
    IdempotencyRecordRepository,
)
from app.infrastructure.database.repositories.outbox_repository import OutboxRepository
from app.infrastructure.database.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW
from app.infrastructure.di import create_container
from app.infrastructure.messaging.kafka_publisher import FastStreamKafkaPublisher
from app.infrastructure.observability import PrometheusIdempotencyObserver
from app.infrastructure.resilience import IdempotencyCircuitBreaker
from app.infrastructure.resilience.circuit_breaking_hot_store import (
    CircuitBreakingHotStore,
)
from app.infrastructure.security.access_token_issuer import PyJWTAccessTokenIssuer
from app.infrastructure.security.access_token_verifier import PyJWTAccessTokenVerifier
from app.infrastructure.security.opaque_refresh_token_codec import (
    SHA256OpaqueRefreshTokenCodec,
)
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.replay_result_protector import (
    AESGCMReplayResultProtector,
)
from app.infrastructure.security.rsa_keys import RSAKeyPair


@pytest.fixture
def test_environment(monkeypatch: pytest.MonkeyPatch) -> Settings:
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path = temp_path / "jwt-private.pem"
        public_path = temp_path / "jwt-public.pem"
        replay_path = temp_path / "replay-v1.key"
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

        monkeypatch.setenv("DATABASE_HOST", "127.0.0.1")
        monkeypatch.setenv("DATABASE_PORT", "5432")
        monkeypatch.setenv("DATABASE_USER", "test_user")
        monkeypatch.setenv("DATABASE_PASSWORD", "test_password")
        monkeypatch.setenv("DATABASE_NAME", "test_db")
        monkeypatch.setenv("VALKEY_HOST", "127.0.0.1")
        monkeypatch.setenv("VALKEY_PORT", "6379")
        monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(private_path))
        monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(public_path))
        monkeypatch.setenv("JWT_ACTIVE_KEY_ID", "test-key")
        monkeypatch.setenv("JWT_ISSUER", "test-issuer")
        monkeypatch.setenv("JWT_SERVICE_AUDIENCE", "test-audience")
        monkeypatch.setenv("JWT_AUDIENCES", "test-audience")
        monkeypatch.setenv("REPLAY_ENCRYPTION_KEY_PATHS", f"test-key={replay_path}")
        monkeypatch.setenv("REPLAY_ENCRYPTION_ACTIVE_KEY_ID", "test-key")

        get_settings.cache_clear()
        yield get_settings()


@pytest.mark.asyncio
async def test_container_resolves_all_settings(
    test_environment: Settings,
) -> None:
    container: AsyncContainer = create_container()
    try:
        assert await container.get(Settings) == test_environment
        assert await container.get(AppSettings) == test_environment.app
        assert await container.get(PostgresSettings) == test_environment.postgres
        assert await container.get(ValkeySettings) == test_environment.valkey
        assert await container.get(KafkaSettings) == test_environment.kafka
        assert await container.get(OutboxSettings) == test_environment.outbox
        assert await container.get(SecuritySettings) == test_environment.security
        assert await container.get(IdempotencySettings) == test_environment.idempotency
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_container_resolves_app_scope_infrastructure(
    test_environment: Settings,
) -> None:
    container: AsyncContainer = create_container()
    try:
        # Database
        assert isinstance(await container.get(AsyncEngine), AsyncEngine)
        sessionmaker_inst = await container.get(async_sessionmaker[AsyncSession])
        assert isinstance(sessionmaker_inst, async_sessionmaker)

        # Messaging & Outbox Relay
        assert isinstance(
            await container.get(BrokerPublisherProtocol),
            FastStreamKafkaPublisher,
        )
        assert callable(await container.get(OutboxScopeFactory))
        assert isinstance(await container.get(OutboxRelayService), OutboxRelayService)

        # Security
        assert isinstance(await container.get(RSAKeyPair), RSAKeyPair)
        assert isinstance(
            await container.get(AccessTokenIssuerProtocol), PyJWTAccessTokenIssuer
        )
        assert isinstance(
            await container.get(AccessTokenVerifierProtocol),
            PyJWTAccessTokenVerifier,
        )
        assert isinstance(
            await container.get(PasswordHasherProtocol), Argon2PasswordHasher
        )
        assert isinstance(
            await container.get(OpaqueRefreshTokenCodecProtocol),
            SHA256OpaqueRefreshTokenCodec,
        )
        assert isinstance(
            await container.get(ReplayResultProtectorProtocol),
            AESGCMReplayResultProtector,
        )

        # Idempotency
        assert isinstance(
            await container.get(IdempotencyObserverProtocol),
            PrometheusIdempotencyObserver,
        )
        assert isinstance(await container.get(Redis), Redis)
        assert isinstance(
            await container.get(IdempotencyCircuitBreaker), IdempotencyCircuitBreaker
        )
        assert isinstance(
            await container.get(HotIdempotencyStoreProtocol),
            CircuitBreakingHotStore,
        )
    finally:
        await container.close()


@pytest.mark.asyncio
async def test_container_resolves_request_scope_dependencies(
    test_environment: Settings,
) -> None:
    container: AsyncContainer = create_container()
    try:
        async with container() as request_container:
            # Database & UOW
            assert isinstance(await request_container.get(AsyncSession), AsyncSession)
            assert isinstance(
                await request_container.get(AsyncUOWProtocol), SQLAlchemyAsyncUOW
            )

            # Repositories
            assert isinstance(
                await request_container.get(UserRepositoryProtocol),
                UserRepository,
            )
            assert isinstance(
                await request_container.get(RefreshTokenRepositoryProtocol),
                RefreshTokenRepository,
            )
            assert isinstance(
                await request_container.get(AuthSessionRepositoryProtocol),
                AuthSessionRepository,
            )
            assert isinstance(
                await request_container.get(OutboxRepositoryProtocol),
                OutboxRepository,
            )
            assert isinstance(
                await request_container.get(
                    EventPublisherProtocol[UserRegisteredEvent]
                ),
                OutboxRepository,
            )

            # Idempotency
            assert isinstance(
                await request_container.get(IdempotencyRecordRepositoryProtocol),
                IdempotencyRecordRepository,
            )
            assert isinstance(
                await request_container.get(DurableExecutionProtocol),
                DurableExecutionService,
            )
            assert isinstance(
                await request_container.get(IdempotencyCoordinatorProtocol),
                IdempotencyCoordinator,
            )

            # Services / Use cases
            assert isinstance(
                await request_container.get(TransactionalRefreshOperationProtocol),
                TransactionalRefreshOperation,
            )
            assert isinstance(
                await request_container.get(RefreshUseCaseProtocol),
                RefreshUseCase,
            )
            assert isinstance(
                await request_container.get(AuthServiceProtocol), AuthService
            )
    finally:
        await container.close()


def test_di_exports_all_scoped_providers() -> None:
    from app.infrastructure import di

    assert hasattr(di, "SettingsProvider")
    assert hasattr(di, "DatabaseAppProvider")
    assert hasattr(di, "DatabaseRequestProvider")
    assert hasattr(di, "SecurityProvider")
    assert hasattr(di, "IdempotencyAppProvider")
    assert hasattr(di, "IdempotencyRequestProvider")
    assert hasattr(di, "RepositoriesProvider")
    assert hasattr(di, "ServicesProvider")
    assert hasattr(di, "MessagingProvider")
    assert hasattr(di, "create_container")
