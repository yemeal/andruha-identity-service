import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Entity(DomainModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid7)
    created_at: datetime = Field(default_factory=utc_now)


class MutableEntity(Entity):
    updated_at: datetime | None = None
