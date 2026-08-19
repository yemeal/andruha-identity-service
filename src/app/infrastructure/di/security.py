from datetime import timedelta

import dishka
from dishka import Provider, Scope

from app.application.ports.idempotency import ReplayResultProtectorProtocol
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    AccessTokenVerifierProtocol,
    OpaqueRefreshTokenCodecProtocol,
    PasswordHasherProtocol,
)
from app.core.settings import SecuritySettings
from app.infrastructure.security.access_token_issuer import PyJWTAccessTokenIssuer
from app.infrastructure.security.access_token_verifier import PyJWTAccessTokenVerifier
from app.infrastructure.security.opaque_refresh_token_codec import (
    SHA256OpaqueRefreshTokenCodec,
)
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.replay_result_protector import (
    AESGCMReplayResultProtector,
    load_replay_key,
)
from app.infrastructure.security.rsa_keys import RSAKeyPair, load_rsa_key_pair


class SecurityProvider(Provider):
    scope = Scope.APP

    @dishka.provide
    def rsa_key_pair(self, settings: SecuritySettings) -> RSAKeyPair:
        return load_rsa_key_pair(
            private_key_path=settings.JWT_PRIVATE_KEY_PATH,
            public_key_path=settings.JWT_PUBLIC_KEY_PATH,
        )

    @dishka.provide
    def issuer(
        self, settings: SecuritySettings, key_pair: RSAKeyPair
    ) -> AccessTokenIssuerProtocol:
        return PyJWTAccessTokenIssuer(
            private_key=key_pair.private_key,
            key_id=settings.JWT_ACTIVE_KEY_ID,
            issuer=settings.JWT_ISSUER,
            audiences=settings.JWT_AUDIENCES,
            access_token_ttl=timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS),
        )

    @dishka.provide
    def verifier(
        self, settings: SecuritySettings, key_pair: RSAKeyPair
    ) -> AccessTokenVerifierProtocol:
        return PyJWTAccessTokenVerifier(
            public_keys={settings.JWT_ACTIVE_KEY_ID: key_pair.public_key},
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_SERVICE_AUDIENCE,
            leeway=timedelta(seconds=settings.JWT_CLOCK_SKEW_SECONDS),
        )

    @dishka.provide
    def password_hasher(self) -> PasswordHasherProtocol:
        return Argon2PasswordHasher()

    @dishka.provide
    def refresh_codec(self) -> OpaqueRefreshTokenCodecProtocol:
        return SHA256OpaqueRefreshTokenCodec()

    @dishka.provide
    def replay_protector(
        self, settings: SecuritySettings
    ) -> ReplayResultProtectorProtocol:
        keys = {
            key_id: load_replay_key(path)
            for key_id, path in settings.REPLAY_ENCRYPTION_KEY_PATHS.items()
        }
        return AESGCMReplayResultProtector(
            active_key_id=settings.REPLAY_ENCRYPTION_ACTIVE_KEY_ID, keys=keys
        )
