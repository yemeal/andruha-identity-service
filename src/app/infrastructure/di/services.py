from datetime import timedelta

import dishka
from dishka import Provider, Scope

from app.application.ports.events import EventPublisherProtocol, UserRegisteredEvent
from app.application.ports.idempotency import (
    IdempotencyCoordinatorProtocol,
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
from app.application.services.refresh import (
    RefreshUseCase,
    RefreshUseCaseProtocol,
    TransactionalRefreshOperation,
    TransactionalRefreshOperationProtocol,
)
from app.core.settings import IdempotencySettings, SecuritySettings


class ServicesProvider(Provider):
    scope = Scope.REQUEST

    @dishka.provide
    def transactional_refresh(
        self,
        users: UserRepositoryProtocol,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        issuer: AccessTokenIssuerProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        settings: SecuritySettings,
    ) -> TransactionalRefreshOperationProtocol:
        return TransactionalRefreshOperation(
            users=users,
            tokens=tokens,
            sessions=sessions,
            issuer=issuer,
            codec=codec,
            protector=protector,
            session_idle_ttl=timedelta(seconds=settings.AUTH_SESSION_IDLE_TTL_SECONDS),
        )

    @dishka.provide
    def refresh_use_case(
        self,
        coordinator: IdempotencyCoordinatorProtocol,
        operation: TransactionalRefreshOperationProtocol,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        uow: AsyncUOWProtocol,
        settings: IdempotencySettings,
    ) -> RefreshUseCaseProtocol:
        return RefreshUseCase(
            coordinator=coordinator,
            operation=operation,
            tokens=tokens,
            sessions=sessions,
            codec=codec,
            protector=protector,
            uow=uow,
            lease_seconds=settings.IDEMPOTENCY_LEASE_SECONDS,
        )

    @dishka.provide
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
        event_publisher: EventPublisherProtocol[UserRegisteredEvent],
        settings: SecuritySettings,
    ) -> AuthServiceProtocol:
        return AuthService(
            user_repo=users,
            refresh_token_repo=tokens,
            auth_session_repo=sessions,
            uow=uow,
            password_hasher=password_hasher,
            access_token_issuer=issuer,
            access_token_verifier=verifier,
            refresh_token_codec=codec,
            event_publisher=event_publisher,
            session_idle_ttl=timedelta(seconds=settings.AUTH_SESSION_IDLE_TTL_SECONDS),
        )
