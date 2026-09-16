from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ModelWrapValidatorHandler,
    ValidationError,
    model_validator,
)

from app.application.exceptions.commands import InvalidCommandError


class UseCaseInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="wrap")
    @classmethod
    def validate_input(
        cls, value: Any, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        try:
            return handler(value)
        except ValidationError:
            raise InvalidCommandError() from None
