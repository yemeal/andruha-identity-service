from typing import Protocol

from app.application.ports.dto.outbox import OutboxMessage


class EventPublisherProtocol[EventT](Protocol):
    """Store an integration event in the active transaction."""

    async def publish(self, event: EventT) -> None:
        """Сохранить интеграционное событие в буфер Outbox текущей сессии БД."""
        ...


class BrokerPublisherProtocol(Protocol):
    """Publish a message and await the broker acknowledgement.

    TransientPublishError permits retry; PermanentPublishError requires quarantine.
    """

    async def publish(self, message: OutboxMessage) -> None:
        """Отправить Outbox-сообщение в брокер и дождаться подтверждения доставки (ACK)."""
        ...
