from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid7

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
import jwt

from app.application.exceptions.security import (
    InvalidTokenConfigurationError,
    TokenIssuanceError,
)
from app.application.ports.dto.security import AccessPrincipal


class PyJWTAccessTokenIssuer:
    """Issue RS256 access tokens using a key loaded once at startup."""

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
            raise InvalidTokenConfigurationError()
        if not normalized_issuer:
            raise InvalidTokenConfigurationError()
        if not normalized_audiences or any(
            not audience for audience in normalized_audiences
        ):
            raise InvalidTokenConfigurationError()
        if access_token_ttl <= timedelta(0):
            raise InvalidTokenConfigurationError()

        self._private_key = private_key
        self._key_id = normalized_key_id
        self._issuer = normalized_issuer
        self._audiences = normalized_audiences
        self._access_token_ttl = access_token_ttl

    def issue(self, principal: AccessPrincipal, now: datetime) -> str:
        """Sign claims with explicit UTC issue time and a fresh token identifier."""
        if now.utcoffset() is None:
            raise TokenIssuanceError()

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
            raise TokenIssuanceError() from error
