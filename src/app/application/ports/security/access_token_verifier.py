from typing import Protocol

from app.application.ports.dto.security import AccessTokenClaims


class AccessTokenVerifierProtocol(Protocol):
    """
    Проверяет access JWT и возвращает только типизированные claims.

    Невалидный, просроченный или неподходящий для этого сервиса токен
    сообщает через доменную ошибку, не возвращая частично проверенные данные.

    Порт синхронный намеренно: RSA-проверка короткая и ограниченная по времени.
    """

    def verify(self, token: str) -> AccessTokenClaims: ...
