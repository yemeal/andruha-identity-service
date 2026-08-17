from datetime import datetime
from typing import Protocol

from app.application.ports.dto.security import (
    AccessPrincipal,
    AccessTokenClaims,
    IssuedRefreshToken,
)


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


class AccessTokenIssuerProtocol(Protocol):
    """
    Выпускает короткоживущий stateless access JWT.

    Токен не содержит sid и не проверяется по состоянию AuthSession. После
    logout он остается действителен до exp, без denylist и запросов в БД.
    """

    def issue(
        self,
        principal: AccessPrincipal,
        now: datetime,
    ) -> str: ...


class AccessTokenVerifierProtocol(Protocol):
    """
    Проверяет access JWT и возвращает только типизированные claims.

    Невалидный, просроченный или неподходящий для этого сервиса токен
    сообщает через доменную ошибку, не возвращая частично проверенные данные.

    Порт синхронный намеренно: RSA-проверка короткая и ограниченная по времени.
    """

    def verify(self, token: str) -> AccessTokenClaims: ...


class OpaqueRefreshTokenCodecProtocol(Protocol):
    """
    Выпускает opaque refresh-токены и вычисляет их SHA-256 digest.

    issue возвращает открытое значение только для ответа клиенту и тот же
    digest, который application-слой сохраняет в БД. digest используется для
    поиска уже выпущенного токена и всегда возвращает 32 байта.
    """

    def issue(self) -> IssuedRefreshToken: ...

    def digest(self, token: str) -> bytes: ...
