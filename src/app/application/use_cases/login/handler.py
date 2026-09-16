from collections.abc import Callable
from datetime import datetime, timedelta

import structlog

from app.application.dto.token_pair import TokenPair
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import PasswordHasherProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.tokens import TokenPairIssuer
from app.application.use_cases.login.command import LoginCommand
from app.domain.aggregates.auth_session import AuthSession
from app.domain.base import utc_now
from app.domain.exceptions import InvalidCredentialsError

logger = structlog.get_logger(__name__)


class LoginHandler:
    def __init__(
        self,
        users: UserRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        uow: AsyncUOWProtocol,
        password_hasher: PasswordHasherProtocol,
        issuer: TokenPairIssuer,
        session_idle_ttl: timedelta,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._uow = uow
        self._password_hasher = password_hasher
        self._issuer = issuer
        self._session_idle_ttl = session_idle_ttl
        self._clock = clock

    async def execute(self, command: LoginCommand) -> TokenPair:
        # Argon2 выполняется после read-транзакции, освобождая соединение из пула.
        async with self._uow:
            candidate = await self._users.get_by_email(command.email)
        password_hash = (
            candidate.password_hash
            if candidate is not None and candidate.can_authenticate
            else None
        )
        verified = await self._password_hasher.verify_or_dummy(
            command.password, password_hash
        )
        if candidate is None or not verified or password_hash is None:
            raise InvalidCredentialsError()

        async with self._uow:
            # После проверки пароля аккаунт мог измениться; перечитываем под lock.
            user = await self._users.get_for_update(candidate.id)
            if (
                user is None
                or not user.can_authenticate
                or user.password_hash != password_hash
            ):
                raise InvalidCredentialsError()
            now = self._clock()
            issued = self._issuer.issue(user, now)
            session = AuthSession.start(
                user_id=user.id,
                token_hash=issued.refresh_digest,
                now=now,
                idle_ttl=self._session_idle_ttl,
            )
            await self._sessions.create(session)
        logger.info("login succeeded")
        return issued.pair
