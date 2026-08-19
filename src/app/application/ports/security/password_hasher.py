from typing import Protocol


class PasswordHasherProtocol(Protocol):
    """Порт хэширования паролей. Должен предоставлять асинхронный интерфейс."""

    async def hash(self, password: str) -> str: ...

    async def verify(self, password: str, password_hash: str) -> bool: ...

    async def verify_or_dummy(
        self,
        password: str,
        password_hash: str | None,
    ) -> bool:
        """
        Всегда выполняет дорогую KDF.

        Если hash отсутствует, адаптер выполняет эквивалентную dummy-работу и
        возвращает False. Это не дает превратить отсутствие пользователя в
        быстрый timing-oracle.
        """
        ...
