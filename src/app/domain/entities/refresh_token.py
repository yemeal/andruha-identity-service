from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.base import Entity, require_aware
from app.domain.exceptions import InvalidTimestampError, RefreshTokenReuseError


class RefreshToken(Entity):
    """Неизменяемая запись одноразового credential внутри AuthSession."""

    session_id: UUID
    token_hash: bytes = Field(min_length=32, max_length=32, strict=True, repr=False)
    used_at: datetime | None = None

    @model_validator(mode="after")
    def validate_used_at(self) -> Self:
        if self.used_at is not None:
            require_aware(self.used_at)
            if self.used_at < self.created_at:
                raise InvalidTimestampError()
        return self

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def _consumed(self, now: datetime) -> Self:
        if self.is_used:
            raise RefreshTokenReuseError()
        return self.model_copy(update={"used_at": now})
