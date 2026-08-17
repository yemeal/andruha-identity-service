from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth_sessions import AuthSession
from app.infrastructure.database.models import AuthSessionORM
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)


class AuthSessionRepository(SQLAlchemyAsyncRepository[AuthSession, AuthSessionORM]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, AuthSession, AuthSessionORM)

    async def get_for_update(self, session_id: UUID) -> AuthSession | None:
        stmt = (
            select(AuthSessionORM)
            .where(AuthSessionORM.id == session_id)
            .with_for_update()
        )

        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()

        if orm_model is None:
            return None

        return AuthSession.model_validate(orm_model, from_attributes=True)

    async def revoke_all_for_user(self, user_id: UUID) -> int:
        # массовый UPDATE вместо выборки по одному: используется при подозрении
        # на утечку (повторное использование отозванного refresh-токена)
        raise NotImplementedError
