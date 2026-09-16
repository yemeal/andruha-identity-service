from typing import Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from app.domain.exceptions import InvalidPasswordHashError


class PasswordHash(str):
    """Непустой результат хеширования; алгоритм принадлежит security-адаптеру."""

    def __new__(cls, value: object) -> PasswordHash:
        if not isinstance(value, str) or not value.strip():
            raise InvalidPasswordHashError()
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema()
        )
