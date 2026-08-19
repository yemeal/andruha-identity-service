from uuid import UUID

from app.application.ports.repositories.base import AsyncRepositoryProtocol
from app.domain.refresh_tokens import RefreshToken


class RefreshTokenRepositoryProtocol(AsyncRepositoryProtocol[RefreshToken, UUID]):
    """Хранилище одноразовых refresh-токенов без собственного TTL."""

    async def get_by_hash_for_update(self, token_hash: bytes) -> RefreshToken | None:
        """SELECT FOR UPDATE: два refresh-запроса не потребляют токен параллельно, пока идет транзакция"""
        ...
