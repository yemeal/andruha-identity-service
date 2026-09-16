from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Self
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    model_validator,
)

from app.domain.exceptions import InvalidDomainStateError, InvalidTimestampError


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware(value: datetime) -> None:
    if value.utcoffset() is None:
        raise InvalidTimestampError()


class DomainModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True, frozen=True, hide_input_in_errors=True, extra="forbid"
    )

    @model_validator(mode="wrap")
    @classmethod
    def validate_state(
        cls, value: Any, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        try:
            return handler(value)
        except ValidationError:
            raise InvalidDomainStateError() from None

    def __setattr__(self, name: str, value: Any) -> None:
        try:
            super().__setattr__(name, value)
        except ValidationError:
            raise InvalidDomainStateError() from None

    def _state(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        state = self._state()
        if deep:
            state = deepcopy(state)
        return type(self).model_validate({**state, **(update or {})})

    def _change_state(self, **changes: Any) -> None:
        """Проверяет полный переход до изменения исходного объекта."""
        candidate = type(self).model_validate({**self._state(), **changes})
        self.__dict__.update(candidate.__dict__)
        self.__pydantic_fields_set__.update(changes)


class Entity(DomainModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid7)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_created_at(self) -> Self:
        require_aware(self.created_at)
        return self


class MutableEntity(Entity):
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_updated_at(self) -> Self:
        if self.updated_at is not None:
            require_aware(self.updated_at)
            if self.updated_at < self.created_at:
                raise InvalidTimestampError()
        return self

    def _change_at(self, now: datetime, **changes: Any) -> None:
        require_aware(now)
        if now < (self.updated_at or self.created_at):
            raise InvalidTimestampError()
        self._change_state(updated_at=now, **changes)
