from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid7

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.application.ports.dto.security import AccessPrincipal
from app.domain.exceptions import DomainErrors


class PyJWTAccessTokenIssuer:
    """
    Issue a short-lived RS256 access JWT.

    Закрытый ключ передается готовым объектом и переиспользуется между
    запросами. Адаптер не читает настройки, файлы и данные пользователя:
    снаружи он получает только параметры токена и минимальный principal.

    Алгоритм является частью реализации, поэтому жестко прописан внутри
    Этим мы не даем конфигурации или заголовку входного JWT переключить подпись
    на другой алгоритм.
    """

    ALGORITHM: Final = "RS256"
    TOKEN_TYPE: Final = "at+jwt"

    def __init__(
        self,
        private_key: RSAPrivateKey,
        key_id: str,
        issuer: str,
        audiences: frozenset[str],
        access_token_ttl: timedelta,
    ) -> None:
        normalized_key_id = key_id.strip()
        normalized_issuer = issuer.strip()
        normalized_audiences = frozenset(audience.strip() for audience in audiences)

        if not normalized_key_id:
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        if not normalized_issuer:
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        if not normalized_audiences or any(
            not audience for audience in normalized_audiences
        ):
            raise DomainErrors.Token.INVALID_CONFIGURATION()
        if access_token_ttl <= timedelta(0):
            raise DomainErrors.Token.INVALID_CONFIGURATION()

        self._private_key = private_key
        self._key_id = normalized_key_id
        self._issuer = normalized_issuer
        self._audiences = normalized_audiences
        self._access_token_ttl = access_token_ttl

    def issue(self, principal: AccessPrincipal, now: datetime) -> str:
        """
        Собрать claims и подписать новый access-токен.

        now передается application-слоем, чтобы время выпуска было явным и
        тестируемым. Перед подписью оно приводится к UTC. jti создается для
        каждого токена отдельно и не используется для серверной инвалидации.
        """
        if now.utcoffset() is None:
            raise DomainErrors.Token.INVALID_DATA()

        issued_at = now.astimezone(UTC)
        expires_at = issued_at + self._access_token_ttl

        payload = {
            "iss": self._issuer,
            "sub": str(principal.user_id),
            "aud": sorted(self._audiences),
            "iat": issued_at,
            "exp": expires_at,
            "jti": str(uuid7()),
            "role": principal.role.value,
        }
        headers = {
            "typ": self.TOKEN_TYPE,
            "kid": self._key_id,
        }

        try:
            return jwt.encode(
                payload,
                self._private_key,
                algorithm=self.ALGORITHM,
                headers=headers,
            )
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise DomainErrors.Token.INVALID() from error
