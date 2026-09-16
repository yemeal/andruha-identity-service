from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Self, cast
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.application.exceptions.security import (
    InvalidTokenDataError,
    TokenIssuanceError,
)
from app.domain.aggregates.user import UserRole


@dataclass(frozen=True, slots=True)
class AccessPrincipal:
    """User identity and role; credentials never reach the JWT adapter."""

    user_id: UUID
    role: UserRole


NonBlankClaim = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AccessTokenClaims(BaseModel):
    """Claims validated by the access-token verifier.

    Access tokens are stateless and carry no authentication-session identifier.
    """

    model_config = ConfigDict(
        frozen=True,
        validate_by_name=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    issuer: NonBlankClaim = Field(validation_alias="iss")
    user_id: UUID = Field(validation_alias="sub")
    audiences: frozenset[NonBlankClaim] = Field(
        validation_alias="aud",
        min_length=1,
    )
    issued_at: AwareDatetime = Field(validation_alias="iat")
    expires_at: AwareDatetime = Field(validation_alias="exp")
    token_id: UUID = Field(validation_alias="jti")
    role: UserRole

    @field_validator("audiences", mode="before")
    @classmethod
    def normalize_audiences(cls, value: object) -> object:
        # RFC допускает строку для одного получателя и массив для нескольких.
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return cast(
                list[object] | tuple[object, ...] | set[object] | frozenset[object],
                value,
            )
        raise InvalidTokenDataError()

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def validate_numeric_date(cls, value: object) -> object:
        # JWT NumericDate claims must remain numeric at the adapter boundary.
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                raise InvalidTokenDataError()
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidTokenDataError()
        return value

    @model_validator(mode="after")
    def validate_time_window(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise InvalidTokenDataError()
        return self


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    """Opaque credential and storage digest; both are excluded from repr."""

    value: str = field(repr=False)
    digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value:
            raise TokenIssuanceError()
        if len(self.digest) != 32:
            raise TokenIssuanceError()
