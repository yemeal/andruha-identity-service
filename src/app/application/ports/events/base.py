import uuid
from datetime import datetime
from typing import Any, Protocol


class IntegrationEventProtocol(Protocol):
    """
    Базовый протокол для всех интеграционных событий системы.

    Определяет минимальный набор атрибутов, необходимых инфраструктурному
    слою Outbox для сохранения, маршрутизации и сериализации события.
    """

    @property
    def event_id(self) -> uuid.UUID:
        """Уникальный идентификатор экземпляра события (UUIDv7)."""
        ...

    @property
    def event_type(self) -> str:
        """
        Канонический строковый тип события согласно контракту
        (например, `identity.user_registered.v1`).
        """
        ...

    @property
    def partition_key(self) -> str:
        """
        Ключ партиционирования для брокера сообщений,
        гарантирующий строгий порядок обработки сообщений одной сущности.
        """
        ...

    @property
    def occurred_at(self) -> datetime:
        """Точное время (UTC timezone-aware) наступления бизнес-события."""
        ...

    def to_envelope_dict(self, producer: str) -> dict[str, Any]:
        """
        Сериализация события в канонический формат JSON Envelope
        в соответствии со спецификацией контракта.
        """
        ...
