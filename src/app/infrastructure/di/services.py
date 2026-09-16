from datetime import timedelta

import dishka
from dishka import Provider, Scope

from app.application.ports.idempotency import (
    IdempotencyCoordinatorProtocol,
    ReplayResultProtectorProtocol,
)
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    AccessTokenVerifierProtocol,
    OpaqueRefreshTokenCodecProtocol,
    PasswordHasherProtocol,
)
from app.application.ports.uow import AsyncUOWProtocol
from app.application.tokens import TokenPairIssuer
from app.application.use_cases.get_current_user.handler import GetCurrentUserHandler
from app.application.use_cases.login.handler import LoginHandler
from app.application.use_cases.logout.handler import LogoutHandler
from app.application.use_cases.refresh.handler import (
    RefreshHandler,
    TransactionalRefreshOperation,
    TransactionalRefreshOperationProtocol,
)
from app.core.settings import IdempotencySettings, SecuritySettings


class ServicesProvider(Provider):
    scope = Scope.REQUEST

    @dishka.provide
    def token_pair_issuer(
        self, issuer: AccessTokenIssuerProtocol, codec: OpaqueRefreshTokenCodecProtocol
    ) -> TokenPairIssuer:
        return TokenPairIssuer(issuer, codec)

    @dishka.provide
    def transactional_refresh(
        self,
        users: UserRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        issuer: TokenPairIssuer,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        settings: SecuritySettings,
    ) -> TransactionalRefreshOperationProtocol:
        return TransactionalRefreshOperation(
            users=users,
            sessions=sessions,
            issuer=issuer,
            codec=codec,
            protector=protector,
            session_idle_ttl=timedelta(seconds=settings.AUTH_SESSION_IDLE_TTL_SECONDS),
        )

    @dishka.provide
    def refresh_handler(
        self,
        coordinator: IdempotencyCoordinatorProtocol,
        operation: TransactionalRefreshOperationProtocol,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        uow: AsyncUOWProtocol,
        settings: IdempotencySettings,
    ) -> RefreshHandler:
        return RefreshHandler(
            coordinator=coordinator,
            operation=operation,
            sessions=sessions,
            codec=codec,
            protector=protector,
            uow=uow,
            lease_seconds=settings.IDEMPOTENCY_LEASE_SECONDS,
        )

    @dishka.provide
    def login_handler(
        self,
        users: UserRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        uow: AsyncUOWProtocol,
        password_hasher: PasswordHasherProtocol,
        issuer: TokenPairIssuer,
        settings: SecuritySettings,
    ) -> LoginHandler:
        return LoginHandler(
            users=users,
            sessions=sessions,
            uow=uow,
            password_hasher=password_hasher,
            issuer=issuer,
            session_idle_ttl=timedelta(seconds=settings.AUTH_SESSION_IDLE_TTL_SECONDS),
        )

    @dishka.provide
    def logout_handler(
        self,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        uow: AsyncUOWProtocol,
    ) -> LogoutHandler:
        return LogoutHandler(sessions=sessions, codec=codec, uow=uow)

    @dishka.provide
    def current_user_handler(
        self,
        users: UserRepositoryProtocol,
        verifier: AccessTokenVerifierProtocol,
        uow: AsyncUOWProtocol,
    ) -> GetCurrentUserHandler:
        return GetCurrentUserHandler(users=users, verifier=verifier, uow=uow)
