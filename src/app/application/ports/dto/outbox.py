from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.base import utc_now


class OutboxStatus(Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUCCESS = "SUCCESS"
    QUARANTINED = "QUARANTINED"


class OutboxKind(Enum):
    """Класс сообщения: команда участнику или факт для шины"""

    COMMAND = "COMMAND"
    EVENT = "EVENT"


class OutboxMessage(BaseModel):
    """
    Единая outbox-запись для команд и событий.

    - `topic` - задает роутинг
    - `key`   - задает порядок сообщений
    - `kind`  - различает команду и факт (событие)
    - `type`  - хранит выбранный commandType/eventType

    id идентифицирует строку outbox; идентификатор сообщения и его envelope принадлежат payload.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid7)
    kind: OutboxKind
    topic: str
    key: str  # partition_key
    type: str  # user.registered.v1 и т.д.
    payload: dict[str, Any]  # полный конверт сообщения
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    last_error_class: str | None = None
    available_at: datetime = Field(default_factory=utc_now)
    claim_token: uuid.UUID | None = None
    claim_expires_at: datetime | None = None
    terminal_at: datetime | None = None
    redrive_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> OutboxMessage:
        if self.available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        if (
            self.claim_expires_at is not None
            and self.claim_expires_at.utcoffset() is None
        ):
            raise ValueError("claim_expires_at must be timezone-aware")
        if self.terminal_at is not None and self.terminal_at.utcoffset() is None:
            raise ValueError("terminal_at must be timezone-aware")

        has_claim = self.claim_token is not None
        has_claim_expiration = self.claim_expires_at is not None
        if has_claim != has_claim_expiration:
            raise ValueError("claim token and expiration must be set together")

        if self.status is OutboxStatus.CLAIMED:
            if not has_claim or self.terminal_at is not None:
                raise ValueError("CLAIMED outbox message requires a live claim")
        elif has_claim:
            raise ValueError("only CLAIMED outbox message may hold a claim")

        if self.status in (OutboxStatus.SUCCESS, OutboxStatus.QUARANTINED):
            if self.terminal_at is None:
                raise ValueError("terminal outbox message requires terminal_at")
        elif self.terminal_at is not None:
            raise ValueError("nonterminal outbox message cannot have terminal_at")
        return self

    @model_validator(mode="after")
    def validate_entity_timestamps(self) -> OutboxMessage:
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.updated_at is not None and self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return self
