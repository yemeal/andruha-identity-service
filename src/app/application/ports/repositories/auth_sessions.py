from typing import Protocol
from uuid import UUID

from app.domain.aggregates.auth_session import AuthSession


class AuthSessionRepositoryProtocol(Protocol):
    """Хранит переход корня и его токенов в транзакции вызывающего use case."""

    async def create(self, session: AuthSession) -> AuthSession: ...

    async def save(self, session: AuthSession) -> None: ...

    async def get_by_refresh_hash_for_update(
        self, token_hash: bytes
    ) -> AuthSession | None: ...

    async def get_by_refresh_id(self, token_id: UUID) -> AuthSession | None: ...
