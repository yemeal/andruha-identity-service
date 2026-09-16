from typing import Any

from pydantic import (
    EmailStr,
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    TypeAdapter,
    ValidationError,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

from app.domain.exceptions import InvalidEmailError

_EMAIL = TypeAdapter(EmailStr)


class NormalizedEmail(str):
    """Проверенный email в единой форме для сравнения и поиска."""

    def __new__(cls, value: object) -> NormalizedEmail:
        try:
            normalized = _EMAIL.validate_python(value).casefold()
        except ValidationError:
            raise InvalidEmailError() from None
        return super().__new__(cls, normalized)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {**handler(schema), "format": "email"}
