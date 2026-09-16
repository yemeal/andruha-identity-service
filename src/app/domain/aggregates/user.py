from datetime import datetime
from enum import StrEnum

from pydantic import Field

from app.domain.base import MutableEntity
from app.domain.value_objects.email import NormalizedEmail
from app.domain.value_objects.password_hash import PasswordHash


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class User(MutableEntity):
    """Корень учётной записи: credentials и разрешение аутентификации."""

    email: NormalizedEmail
    password_hash: PasswordHash = Field(repr=False)
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE

    @property
    def can_authenticate(self) -> bool:
        return self.status is UserStatus.ACTIVE

    def disable(self, now: datetime) -> None:
        if self.status is not UserStatus.DISABLED:
            self._change_at(now, status=UserStatus.DISABLED)
