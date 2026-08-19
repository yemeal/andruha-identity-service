from uuid import UUID

from app.application.ports.repositories.base import AsyncRepositoryProtocol
from app.domain.auth_sessions import AuthSession


class AuthSessionRepositoryProtocol(AsyncRepositoryProtocol[AuthSession, UUID]):
    """Store sliding-idle authentication sessions."""

    async def get_for_update(self, session_id: UUID) -> AuthSession | None:
        """SELECT FOR UPDATE на время refresh/logout."""
        ...

    async def revoke_all_for_user(self, user_id: UUID) -> int:
        """Отозвать все сессии пользователя."""
        ...
