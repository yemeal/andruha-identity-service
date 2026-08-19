from typing import Protocol


class AsyncRepositoryProtocol[EntityT, EntityIdT](Protocol):
    """Базовый протокол для всех репозиториев"""

    async def create(self, entity: EntityT) -> EntityT:
        """Может вызвать ошибку, если такая сущность уже существует (уникальные поля)"""
        ...

    async def get(self, entity_id: EntityIdT) -> EntityT | None: ...

    async def update(self, entity: EntityT) -> EntityT: ...
