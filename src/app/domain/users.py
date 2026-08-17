from datetime import datetime
from enum import StrEnum

from app.domain.base import MutableEntity
from app.domain.value_objects.email import NormalizedEmail


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class User(MutableEntity):
    """
    Доменная модель пользователя,
    пароль хранится в захешированном виде
    """

    email: NormalizedEmail  # email обязательно UNIQUE
    password_hash: str

    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE

    @property
    def can_authenticate(self) -> bool:
        return self.status is UserStatus.ACTIVE

    def disable(self, now: datetime) -> None:
        self.status = UserStatus.DISABLED
        self.updated_at = now
