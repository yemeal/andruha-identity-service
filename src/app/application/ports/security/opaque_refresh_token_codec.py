from typing import Protocol

from app.application.ports.dto.security import IssuedRefreshToken


class OpaqueRefreshTokenCodecProtocol(Protocol):
    """
    Выпускает opaque refresh-токены и вычисляет их SHA-256 digest.

    issue возвращает открытое значение только для ответа клиенту и тот же
    digest, который application-слой сохраняет в БД. digest используется для
    поиска уже выпущенного токена и всегда возвращает 32 байта.
    """

    def issue(self) -> IssuedRefreshToken: ...

    def digest(self, token: str) -> bytes: ...
