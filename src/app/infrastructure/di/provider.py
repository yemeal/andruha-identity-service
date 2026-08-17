from collections.abc import AsyncIterator
from datetime import timedelta
from typing import cast

from dishka import (
    Provider,
    Scope,
    provide,  # pyright: ignore[reportUnknownVariableType]
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.ports.idempotency import (
    DurableExecutionProtocol,
    HotIdempotencyStoreProtocol,
    IdempotencyCoordinatorProtocol,
    IdempotencyObserverProtocol,
    IdempotencyRecordRepositoryProtocol,
    ReplayResultProtectorProtocol,
)
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
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
from app.application.services.refresh import (
    RefreshUseCase,
    TransactionalRefreshOperation,
)
from app.core.settings import Settings
from app.infrastructure.cache import ValkeyHotIdempotencyStore
from app.infrastructure.database.repositories.auth_session_repository import (
    AuthSessionRepository,
)
from app.infrastructure.database.repositories.idempotency_record_repository import (
    IdempotencyRecordRepository,
)
from app.infrastructure.database.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW
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
    load_replay_key,
)
from app.infrastructure.security.rsa_keys import RSAKeyPair, load_rsa_key_pair


class SettingsProvider(Provider):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        return self._settings


class AppProvider(Provider):
    @provide(scope=Scope.APP)
    def idempotency_observer(self) -> IdempotencyObserverProtocol:
        return PrometheusIdempotencyObserver()

    @provide(scope=Scope.APP)
    def engine(self, settings: Settings) -> AsyncEngine:
        return create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)

    @provide(scope=Scope.APP)
    def sessionmaker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(engine, expire_on_commit=False)

    @provide(scope=Scope.APP)
    async def valkey(self, settings: Settings) -> AsyncIterator[Redis]:
        client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            settings.VALKEY_URL, decode_responses=False
        )
        try:
            yield client
        finally:
            await client.aclose()

    @provide(scope=Scope.APP)
    def circuit_breaker(self, settings: Settings) -> IdempotencyCircuitBreaker:
        return IdempotencyCircuitBreaker(
            settings.IDEMPOTENCY_CB_FAILURES,
            settings.IDEMPOTENCY_CB_RECOVERY_SECONDS,
            "identity-idempotency-valkey",
        )

    @provide(scope=Scope.APP)
    def hot_store(
        self,
        client: Redis,
        circuit_breaker: IdempotencyCircuitBreaker,
        settings: Settings,
    ) -> HotIdempotencyStoreProtocol:
        inner = ValkeyHotIdempotencyStore(
            client,
            result_ttl_seconds=settings.IDEMPOTENCY_RESULT_TTL_SECONDS,
            key_namespace=settings.VALKEY_KEY_NAMESPACE,
        )
        return CircuitBreakingHotStore(inner, circuit_breaker)

    @provide(scope=Scope.APP)
    def rsa_key_pair(self, settings: Settings) -> RSAKeyPair:
        return load_rsa_key_pair(
            private_key_path=settings.JWT_PRIVATE_KEY_PATH,
            public_key_path=settings.JWT_PUBLIC_KEY_PATH,
        )

    @provide(scope=Scope.APP)
    def issuer(
        self, settings: Settings, key_pair: RSAKeyPair
    ) -> AccessTokenIssuerProtocol:
        return PyJWTAccessTokenIssuer(
            private_key=key_pair.private_key,
            key_id=settings.JWT_ACTIVE_KEY_ID,
            issuer=settings.JWT_ISSUER,
            audiences=settings.JWT_AUDIENCES,
            access_token_ttl=timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS),
        )

    @provide(scope=Scope.APP)
    def verifier(
        self, settings: Settings, key_pair: RSAKeyPair
    ) -> AccessTokenVerifierProtocol:
        return PyJWTAccessTokenVerifier(
            public_keys={settings.JWT_ACTIVE_KEY_ID: key_pair.public_key},
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_SERVICE_AUDIENCE,
            leeway=timedelta(seconds=settings.JWT_CLOCK_SKEW_SECONDS),
        )

    @provide(scope=Scope.APP)
    def password_hasher(self) -> PasswordHasherProtocol:
        return Argon2PasswordHasher()

    @provide(scope=Scope.APP)
    def refresh_codec(self) -> OpaqueRefreshTokenCodecProtocol:
        return SHA256OpaqueRefreshTokenCodec()

    @provide(scope=Scope.APP)
    def replay_protector(self, settings: Settings) -> ReplayResultProtectorProtocol:
        keys = {
            key_id: load_replay_key(path)
            for key_id, path in settings.REPLAY_ENCRYPTION_KEY_PATHS.items()
        }
        return AESGCMReplayResultProtector(
            active_key_id=settings.REPLAY_ENCRYPTION_ACTIVE_KEY_ID, keys=keys
        )


class RequestProvider(Provider):
    scope = Scope.REQUEST

    @provide
    async def session(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    @provide
    def uow(self, session: AsyncSession) -> AsyncUOWProtocol:
        return SQLAlchemyAsyncUOW(session)

    @provide
    def users(self, session: AsyncSession) -> UserRepositoryProtocol:
        return cast(UserRepositoryProtocol, UserRepository(session))

    @provide
    def tokens(self, session: AsyncSession) -> RefreshTokenRepositoryProtocol:
        return cast(RefreshTokenRepositoryProtocol, RefreshTokenRepository(session))

    @provide
    def sessions(self, session: AsyncSession) -> AuthSessionRepositoryProtocol:
        return cast(AuthSessionRepositoryProtocol, AuthSessionRepository(session))

    @provide
    def records(
        self, session: AsyncSession, settings: Settings
    ) -> IdempotencyRecordRepositoryProtocol:
        return IdempotencyRecordRepository(
            session, retention_seconds=settings.IDEMPOTENCY_RESULT_TTL_SECONDS
        )

    @provide
    def durable(
        self, records: IdempotencyRecordRepositoryProtocol, uow: AsyncUOWProtocol
    ) -> DurableExecutionProtocol:
        return DurableExecutionService(records, uow)

    @provide
    def coordinator(
        self,
        hot_store: HotIdempotencyStoreProtocol,
        durable: DurableExecutionProtocol,
        observer: IdempotencyObserverProtocol,
    ) -> IdempotencyCoordinatorProtocol:
        return IdempotencyCoordinator(hot_store, durable, observer=observer)

    @provide
    def transactional_refresh(
        self,
        users: UserRepositoryProtocol,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        issuer: AccessTokenIssuerProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        settings: Settings,
    ) -> TransactionalRefreshOperation:
        return TransactionalRefreshOperation(
            users, tokens, sessions, issuer, codec, protector, settings
        )

    @provide
    def refresh_use_case(
        self,
        coordinator: IdempotencyCoordinatorProtocol,
        operation: TransactionalRefreshOperation,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        uow: AsyncUOWProtocol,
        settings: Settings,
    ) -> RefreshUseCase:
        return RefreshUseCase(
            coordinator, operation, tokens, sessions, codec, protector, uow, settings
        )

    @provide
    def auth_service(
        self,
        users: UserRepositoryProtocol,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        uow: AsyncUOWProtocol,
        password_hasher: PasswordHasherProtocol,
        issuer: AccessTokenIssuerProtocol,
        verifier: AccessTokenVerifierProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        settings: Settings,
    ) -> AuthServiceProtocol:
        return AuthService(
            users,
            tokens,
            sessions,
            uow,
            password_hasher,
            issuer,
            verifier,
            codec,
            settings,
        )
