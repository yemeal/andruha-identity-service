from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.domain.base import Entity
from app.domain.exceptions import DomainErrors


class RefreshToken(Entity):
    """
    Доменная модель refresh-токена.

    Access tokens are stateless and never stored in the database.
    Only the SHA-256 refresh-token digest is persisted.

    The refresh token has no independent TTL. It is single-use and valid while
    не использован и пока активна связанная AuthSession. Успешная ротация
    помечает старый токен использованным, создает новый и продлевает idle-срок
    той же сессии.
    """

    session_id: UUID
    token_hash: bytes = Field(min_length=32, max_length=32, strict=True)
    used_at: datetime | None = None

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def consume(self, now: datetime) -> None:
        if self.used_at is not None:
            raise DomainErrors.Session.REFRESH_TOKEN_REUSED()
        self.used_at = now
