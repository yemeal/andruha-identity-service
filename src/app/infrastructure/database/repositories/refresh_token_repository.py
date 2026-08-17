from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.refresh_tokens import RefreshToken
from app.infrastructure.database.models.refresh_tokens import RefreshTokenORM
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)


class RefreshTokenRepository(SQLAlchemyAsyncRepository[RefreshToken, RefreshTokenORM]):
    """Специализированный репозиторий для RefreshToken"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RefreshToken, RefreshTokenORM)

    async def get_by_hash_for_update(self, token_hash: bytes) -> RefreshToken | None:
        stmt = (
            select(RefreshTokenORM)
            .where(RefreshTokenORM.token_hash == token_hash)
            .with_for_update()
        )

        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()

        if orm_model is None:
            return None

        return RefreshToken.model_validate(orm_model, from_attributes=True)
