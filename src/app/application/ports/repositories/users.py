from uuid import UUID

from app.application.ports.repositories.base import AsyncRepositoryProtocol
from app.domain.users import User
from app.domain.value_objects.email import NormalizedEmail


class UserRepositoryProtocol(AsyncRepositoryProtocol[User, UUID]):
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
