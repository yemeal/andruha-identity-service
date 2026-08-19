from datetime import datetime
from typing import Protocol
import uuid


class OutboxRelayObserverProtocol(Protocol):
    """Наблюдатель за жизненным циклом Outbox Relay для метрик Prometheus."""

    def batch_claimed(
        self,
        *,
        count: int,
        oldest_created_at: datetime,
        observed_at: datetime,
    ) -> None: ...

    def message_finalized(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
    ) -> None: ...

    def message_retry_scheduled(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
        attempt: int,
        delay_seconds: float,
    ) -> None: ...

    def message_quarantined(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
        error_class: str,
    ) -> None: ...
