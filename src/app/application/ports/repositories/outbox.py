import uuid
from datetime import datetime

from app.application.ports.dto.outbox import OutboxMessage
from app.application.ports.repositories.base import AsyncRepositoryProtocol


class OutboxRepositoryProtocol(AsyncRepositoryProtocol[OutboxMessage, uuid.UUID]):
    """Durable lifecycle единой outbox-таблицы команд и событий."""

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[OutboxMessage]: ...

    async def finalize_published(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        published_at: datetime,
    ) -> bool: ...

    async def schedule_retry(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        available_at: datetime,
        error_class: str = "TransientPublishError",
    ) -> bool: ...

    async def quarantine(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        quarantined_at: datetime,
        error_class: str = "PermanentOutboxPublishError",
    ) -> bool: ...

    async def redrive_quarantined(
        self,
        message_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool: ...

    async def delete_terminal_before(
        self,
        *,
        terminal_at: datetime,
        limit: int,
    ) -> int: ...
