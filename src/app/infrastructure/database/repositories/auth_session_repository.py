from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.exceptions.persistence import StoredStateError
from app.domain.aggregates.auth_session import AuthSession
from app.domain.entities.refresh_token import RefreshToken
from app.domain.exceptions import InvalidDomainStateError
from app.infrastructure.database.models import AuthSessionORM
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)
from app.infrastructure.database.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)


class AuthSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._sessions = SQLAlchemyAsyncRepository(session, AuthSession, AuthSessionORM)
        self._tokens = RefreshTokenRepository(session)

    async def create(self, session: AuthSession) -> AuthSession:
        await self._sessions.create(session)
        if session.refresh_token is not None:
            await self._tokens.create(session.refresh_token)
        return session

    async def save(self, session: AuthSession) -> None:
        await self._sessions.update(session)
        if session.refresh_token is not None:
            await self._tokens.update(session.refresh_token)
        if session.replacement_token is not None:
            await self._tokens.create(session.replacement_token)

    async def get_by_refresh_hash_for_update(
        self, token_hash: bytes
    ) -> AuthSession | None:
        # Единый порядок блокировок для refresh/logout: сначала token, затем session.
        token = await self._tokens.get_by_hash_for_update(token_hash)
        if token is None:
            return None
        row = await self._session.scalar(
            select(AuthSessionORM)
            .where(AuthSessionORM.id == token.session_id)
            .with_for_update()
        )
        return self._with_token(row, token)

    async def get_by_refresh_id(self, token_id: UUID) -> AuthSession | None:
        token = await self._tokens.get(token_id)
        if token is None:
            return None
        row = await self._session.get(AuthSessionORM, token.session_id)
        return self._with_token(row, token)

    @staticmethod
    def _with_token(
        row: AuthSessionORM | None, token: RefreshToken
    ) -> AuthSession | None:
        if row is None:
            return None
        try:
            session = AuthSession.model_validate(row)
            return session.model_copy(update={"refresh_token": token})
        except InvalidDomainStateError as error:
            raise StoredStateError() from error
