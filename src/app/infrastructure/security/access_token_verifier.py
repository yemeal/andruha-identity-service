from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Final

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
import jwt
from pydantic import ValidationError

from app.application.exceptions.security import (
    InvalidTokenConfigurationError,
    InvalidTokenDataError,
    InvalidTokenSigningKeyError,
    TokenExpiredError,
    TokenMalformedError,
)
from app.application.ports.dto.security import AccessTokenClaims
from app.infrastructure.security.rsa_keys import MIN_RSA_KEY_SIZE_BITS


class PyJWTAccessTokenVerifier:
    """
    Проверяет access JWT публичными RSA-ключами.

    Ключ выбирается только из локального key ring по `kid`. Значение из токена
    никогда не превращается в путь или URL. Алгоритм, тип токена, issuer и
    Audience is part of the verifier's trusted configuration.

    Адаптер не обращается к БД и AuthSession: access-токен остается stateless.
    """

    ALGORITHM: Final = "RS256"
    TOKEN_TYPE: Final = "at+jwt"
    REQUIRED_CLAIMS: Final = (
        "iss",
        "sub",
        "aud",
        "iat",
        "exp",
        "jti",
        "role",
    )

    def __init__(
        self,
        # public keys - локальный key ring (таблица соответствия):
        # kid -> публичный RSA-ключ
        # нужен для ротации публичных ключей без простаивания
        public_keys: Mapping[str, RSAPublicKey],
        issuer: str,
        audience: str,
        leeway: timedelta | None = None,
    ) -> None:
        effective_leeway = leeway if leeway is not None else timedelta(0)
        normalized_issuer = issuer.strip()
        normalized_audience = audience.strip()

        if not normalized_issuer or not normalized_audience:
            raise InvalidTokenConfigurationError()
        if effective_leeway < timedelta(0):
            raise InvalidTokenConfigurationError()
        if not public_keys:
            raise InvalidTokenConfigurationError()

        normalized_public_keys: dict[str, RSAPublicKey] = {}
        for key_id, public_key in public_keys.items():
            normalized_key_id = key_id.strip()
            if not normalized_key_id:
                raise InvalidTokenConfigurationError()
            if normalized_key_id in normalized_public_keys:
                raise InvalidTokenConfigurationError()
            if public_key.key_size < MIN_RSA_KEY_SIZE_BITS:
                raise InvalidTokenSigningKeyError()
            normalized_public_keys[normalized_key_id] = public_key

        # MappingProxyType - read-only view
        # он не дает внешнему коду подменить доверенные ключи после startup
        self._public_keys = MappingProxyType(normalized_public_keys)
        self._issuer = normalized_issuer
        self._audience = normalized_audience
        self._leeway = effective_leeway

    def verify(self, token: str) -> AccessTokenClaims:
        """
        Полностью проверить JWT и вернуть нормализованный DTO.

        No payload field leaves this adapter before signature validation.
        Every PyJWT failure becomes a domain error.
        """
        # Header пока не считается проверенным: из него берем только kid для
        # поиска в уже доверенном локальном key ring.
        public_key = self._select_public_key(token)

        try:
            payload = jwt.decode(
                token,
                public_key,
                # Алгоритм не берем из JWT, иначе атакующий сможет влиять на
                # способ проверки собственной подписи.
                algorithms=[self.ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={
                    "require": list(self.REQUIRED_CLAIMS),
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as error:
            raise TokenExpiredError() from error
        except (jwt.PyJWTError, TypeError, ValueError, OverflowError) as error:
            raise TokenMalformedError() from error

        # До этой точки подпись и стандартные claims уже проверены. Здесь
        # остается привести payload к строгому внутреннему контракту.
        try:
            return AccessTokenClaims.model_validate(payload)
        except ValidationError as error:
            raise InvalidTokenDataError() from error

    def _select_public_key(self, token: str) -> RSAPublicKey:
        if not token:
            raise TokenMalformedError()

        try:
            # Read only kid before selecting a trusted local public key.
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise TokenMalformedError() from error

        # -> проверить alg=RS256
        if header.get("alg") != self.ALGORITHM:
            raise TokenMalformedError()
        # -> проверить typ=at+jwt
        if header.get("typ") != self.TOKEN_TYPE:
            raise TokenMalformedError()

        key_id = header.get("kid")
        if not isinstance(key_id, str):
            raise TokenMalformedError()

        # Никаких чтений файлов или запросов по входному kid: неизвестный ключ
        # просто означает, что этот сервис токену не доверяет.
        public_key = self._public_keys.get(key_id)
        if public_key is None:
            raise TokenMalformedError()
        return public_key
