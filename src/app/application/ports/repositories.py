from typing import Protocol
from uuid import UUID

from app.domain.auth_sessions import AuthSession
from app.domain.refresh_tokens import RefreshToken
from app.domain.users import User
from app.domain.value_objects.email import NormalizedEmail


class AsyncRepositoryProtocol[EntityT](Protocol):
    """Базовый протокол для всех репозиториев"""

    async def create(self, entity: EntityT) -> EntityT:
        """Может вызвать ошибку, если такая сущность уже существует (уникальные поля)"""
        ...

    async def get(self, entity_id: UUID) -> EntityT | None: ...

    async def update(self, entity: EntityT) -> EntityT: ...


class UserRepositoryProtocol(AsyncRepositoryProtocol[User]):
    """Протокол, специфичный для User"""

    async def create_if_absent(self, user: User) -> User | None:
        """
        Атомарный INSERT ON CONFLICT(email) DO NOTHING.
        Проглатывает возможные конфликты, возвращая None
        """
        ...

    async def get_by_email(self, email: NormalizedEmail) -> User | None: ...

    async def get_for_update(self, user_id: UUID) -> User | None:
        """Повторно прочитать и заблокировать user перед выпуском токенов."""
        ...


class RefreshTokenRepositoryProtocol(AsyncRepositoryProtocol[RefreshToken]):
    """Хранилище одноразовых refresh-токенов без собственного TTL."""

    async def get_by_hash_for_update(self, token_hash: bytes) -> RefreshToken | None:
        """SELECT FOR UPDATE: два refresh-запроса не потребляют токен параллельно, пока идет транзакция"""
        ...


class AuthSessionRepositoryProtocol(AsyncRepositoryProtocol[AuthSession]):
    """Store sliding-idle authentication sessions."""

    async def get_for_update(self, session_id: UUID) -> AuthSession | None:
        """SELECT FOR UPDATE на время refresh/logout."""
        ...

    async def revoke_all_for_user(self, user_id: UUID) -> int:
        """Отозвать все сессии пользователя."""
        ...
