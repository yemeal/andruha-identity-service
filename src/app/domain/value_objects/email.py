from typing import Annotated

from pydantic import AfterValidator, EmailStr


def _normalize_email(value: str) -> str:
    """Приводит email к единой форме для хранения и поиска пользователя."""
    return value.casefold()


NormalizedEmail = Annotated[EmailStr, AfterValidator(_normalize_email)]
