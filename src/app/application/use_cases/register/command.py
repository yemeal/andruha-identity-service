from pydantic import Field, field_validator

from app.application.use_cases.base import UseCaseInput
from app.domain.policies.registration_password import validate_registration_password
from app.domain.value_objects.email import NormalizedEmail


class RegisterUserCommand(UseCaseInput):
    email: NormalizedEmail
    password: str = Field(repr=False)
    key_hash: bytes = Field(min_length=32, max_length=32, strict=True, repr=False)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        return validate_registration_password(value)
