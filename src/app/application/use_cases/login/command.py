from pydantic import Field

from app.application.use_cases.base import UseCaseInput
from app.domain.value_objects.email import NormalizedEmail


class LoginCommand(UseCaseInput):
    email: NormalizedEmail
    password: str = Field(min_length=1, max_length=128, repr=False)
