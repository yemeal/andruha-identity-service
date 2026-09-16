from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.exceptions.persistence import EntityNotFoundError, StoredStateError
from app.domain.exceptions import InvalidDomainStateError


class SQLAlchemyAsyncRepository[DomainModelT: BaseModel, ORMModelT, DomainIdT = UUID]:
    def __init__(
        self,
        session: AsyncSession,
        domain_model: type[DomainModelT],
        orm_model: type[ORMModelT],
    ) -> None:
        self._session = session
        self._domain_model = domain_model
        self._orm_model = orm_model

    def _to_domain(self, orm_model: ORMModelT) -> DomainModelT:
        try:
            return self._domain_model.model_validate(orm_model, from_attributes=True)
        except (InvalidDomainStateError, ValidationError) as error:
            raise StoredStateError() from error

    async def create(self, entity: DomainModelT) -> DomainModelT:
        orm_model = self._orm_model(**entity.model_dump())
        self._session.add(orm_model)
        await self._session.flush()
        return self._to_domain(orm_model)

    async def get(self, entity_id: DomainIdT) -> DomainModelT | None:
        orm_model = await self._session.get(entity=self._orm_model, ident=entity_id)
        if not orm_model:
            return None
        return self._to_domain(orm_model)

    async def update(self, entity: DomainModelT) -> DomainModelT:
        # Hold the row lock until the surrounding transaction ends.
        orm_id = cast(Any, self._orm_model).id
        entity_id = cast(Any, entity).id
        stmt = select(self._orm_model).where(orm_id == entity_id).with_for_update()
        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()
        if not orm_model:
            raise EntityNotFoundError()

        for key, value in entity.model_dump().items():
            setattr(orm_model, key, value)

        await self._session.flush()
        return self._to_domain(orm_model)
