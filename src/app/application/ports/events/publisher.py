from typing import Protocol

from app.application.ports.dto.outbox import OutboxMessage


class EventPublisherProtocol[EventT](Protocol):
    """
    Этап 1: Порт публикации интеграционных событий из Use Case слоя в Outbox-буфер БД.

    Используется бизнес-сервисами (например, AuthService) внутри активной
    транзакции базы данных (Unit of Work). Превращает типизированный объект
    события (например, UserRegisteredEvent) в запись таблицы outbox в PostgreSQL.
    """

    async def publish(self, event: EventT) -> None:
        """Сохранить интеграционное событие в буфер Outbox текущей сессии БД."""
        ...


class BrokerPublisherProtocol(Protocol):
    """
    Этап 2: Порт физической отправки сообщений из Outbox в брокер сообщений (Kafka).

    Используется фоновым процессом OutboxRelay. Принимает DTO OutboxMessage,
    извлеченный из базы данных, и отправляет его по сети в брокер сообщений
    (через FastStream/aiokafka) с ожиданием подтверждения (ACK).

    Raises:
        TransientPublishError: При временной сетевой ошибке брокера (требуется ретрай).
        PermanentPublishError: При неисправимой ошибке схемы/сообщения (карантин).
    """

    async def publish(self, message: OutboxMessage) -> None:
        """Отправить Outbox-сообщение в брокер и дождаться подтверждения доставки (ACK)."""
        ...
