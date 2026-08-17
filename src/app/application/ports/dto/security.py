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

from app.domain.exceptions import DomainErrors
from app.domain.users import UserRole


@dataclass(frozen=True, slots=True)
class AccessPrincipal:
    """
    Минимальный снимок пользователя, необходимый для выпуска access-токена.

    JWT-адаптер не получает целого User и поэтому не видит password_hash,
    email и другие данные, которые не должны попадать в токен.
    """

    user_id: UUID
    role: UserRole


NonBlankClaim = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AccessTokenClaims(BaseModel):
    """
    Validated and normalized access-token claims.

    The adapter creates this model only after signature, algorithm, type,
    issuer, audience, required-claim, and lifetime validation. Access tokens
    remain stateless and are not linked to AuthSession through a sid claim.

    Назначение полей:
        iss - кто выпустил токен;
        sub - стабильный ID пользователя;
        aud - сервисы, которым разрешено принять токен;
        iat и exp - время выпуска и истечения;
        jti - уникальный ID конкретного access-токена;
        role - минимально необходимая авторизационная информация.
    """

    model_config = ConfigDict(
        frozen=True,
        validate_by_name=True,
        extra="ignore",
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
        raise DomainErrors.Token.INVALID_DATA()

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def validate_numeric_date(cls, value: object) -> object:
        # JWT NumericDate claims must remain numeric at the adapter boundary.
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                raise DomainErrors.Token.INVALID_DATA()
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DomainErrors.Token.INVALID_DATA()
        return value

    @model_validator(mode="after")
    def validate_time_window(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise DomainErrors.Token.INVALID_DATA()
        return self


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    """
    Результат выпуска opaque refresh-токена.

    value возвращается клиенту ровно один раз, digest сохраняется в БД.
    Открытое значение исключено из repr, чтобы не утечь в логи.
    """

    value: str = field(repr=False)
    digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value:
            raise DomainErrors.Token.INVALID_DATA()
        if len(self.digest) != 32:
            raise DomainErrors.Token.INVALID_DATA()
