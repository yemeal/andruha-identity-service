from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class SQLAlchemyAsyncRepository[DomainModelT: BaseModel, ORMModelT]:
    def __init__(
        self,
        session: AsyncSession,
        domain_model: type[DomainModelT],
        orm_model: type[ORMModelT],
    ) -> None:
        self._session = session
        self._domain_model = domain_model
        self._orm_model = orm_model

    async def create(self, entity: DomainModelT) -> DomainModelT:
        orm_model = self._orm_model(**entity.model_dump())
        self._session.add(orm_model)
        await self._session.flush()
        return self._domain_model.model_validate(orm_model, from_attributes=True)

    async def get(self, entity_id: UUID) -> DomainModelT | None:
        orm_model = await self._session.get(entity=self._orm_model, ident=entity_id)
        if not orm_model:
            return None
        return self._domain_model.model_validate(orm_model, from_attributes=True)

    async def update(self, entity: DomainModelT) -> DomainModelT:
        # обазетельно юзаем SELECT FOR UPDATE дабы избежать race condition
        orm_id = cast(Any, self._orm_model).id
        entity_id = cast(Any, entity).id
        stmt = select(self._orm_model).where(orm_id == entity_id).with_for_update()
        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()
        if not orm_model:
            raise ValueError(
                f"{self._domain_model.__name__} with id {entity_id} not found for update"
            )

        for key, value in entity.model_dump().items():
            setattr(orm_model, key, value)

        await self._session.flush()
        return self._domain_model.model_validate(orm_model, from_attributes=True)
