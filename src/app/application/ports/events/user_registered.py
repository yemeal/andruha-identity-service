from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.domain.base import utc_now


class UserRegisteredPayload(BaseModel):
    """
    Payload события регистрации пользователя в соответствии с
    contracts/identity/events/user-registered.v1.schema.json.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: uuid.UUID
    registered_at: datetime


class UserRegisteredEvent(BaseModel):
    """
    Интеграционное событие, представляющее успешную регистрацию пользователя.
    в соответствии с contracts/identity/events/user-registered.v1.schema.json.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: uuid.UUID
    registered_at: datetime = Field(default_factory=utc_now)
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid7)
    causation_id: uuid.UUID | None = None
    event_id: uuid.UUID = Field(default_factory=uuid.uuid7)

    @property
    def event_type(self) -> str:
        return "identity.user_registered.v1"

    @property
    def partition_key(self) -> str:
        return str(self.user_id)

    @property
    def occurred_at(self) -> datetime:
        return self.registered_at

    def to_envelope_dict(self, producer: str) -> dict[str, Any]:
        return {
            "eventId": str(self.event_id),
            "eventType": self.event_type,
            "schemaVersion": 1,
            "occurredAt": self.occurred_at.isoformat(),
            "producer": producer,
            "correlationId": str(self.correlation_id),
            "causationId": str(self.causation_id or self.correlation_id),
            "payload": {
                "userId": str(self.user_id),
                "registeredAt": self.registered_at.isoformat(),
            },
        }
