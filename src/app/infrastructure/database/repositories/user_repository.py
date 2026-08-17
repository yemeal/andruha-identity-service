from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.users import User
from app.domain.value_objects.email import NormalizedEmail
from app.infrastructure.database.models.users import UserORM
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)


class UserRepository(SQLAlchemyAsyncRepository[User, UserORM]):
    """Специализированный репозиторий для User"""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, User, UserORM)

    async def create_if_absent(self, user: User) -> User | None:
        stmt = (
            pg_insert(UserORM)
            .values(**user.model_dump())
            .on_conflict_do_nothing(index_elements=[UserORM.email])
            .returning(UserORM)
        )

        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()

        if orm_model is None:
            return None

        return User.model_validate(orm_model, from_attributes=True)

    async def get_by_email(self, email: NormalizedEmail) -> User | None:
        stmt = select(UserORM).where(UserORM.email == email)

        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()

        if orm_model is None:
            return None

        return User.model_validate(orm_model, from_attributes=True)

    async def get_for_update(self, user_id: UUID) -> User | None:
        stmt = select(UserORM).where(UserORM.id == user_id).with_for_update()

        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()

        if orm_model is None:
            return None

        return User.model_validate(orm_model, from_attributes=True)
