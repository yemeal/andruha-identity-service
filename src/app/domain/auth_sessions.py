from datetime import datetime, timedelta
from uuid import UUID

from app.domain.base import Entity
from app.domain.exceptions import DomainErrors


class AuthSession(Entity):
    """
    Сессия объединяет цепочку одноразовых refresh-токенов одного входа.

    `idle_expires_at` - скользящий срок бездействия. Каждый успешный refresh
    продлевает ту же сессию на `idle_ttl`. Абсолютного срока жизни нет:
    пока пользователь регулярно обновляет токены, сессия может жить бессрочно.
    """

    user_id: UUID
    idle_expires_at: datetime
    revoked_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.idle_expires_at

    def extend_idle(self, now: datetime, idle_ttl: timedelta) -> None:
        """
        Продлить активную сессию от времени успешного refresh-запроса.

        Истекшую или отозванную сессию нельзя оживить ротацией токена.
        """
        if not self.is_active(now):
            raise DomainErrors.Session.INACTIVE()
        self.idle_expires_at = now + idle_ttl

    def revoke(self, now: datetime) -> None:
        if self.revoked_at is None:
            self.revoked_at = now
