from app.application.exceptions.security import (
    InvalidTokenConfigurationError,
    InvalidTokenDataError,
    InvalidTokenError,
    InvalidTokenSigningKeyError,
    TokenExpiredError,
    TokenMalformedError,
)
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from pydantic import ValidationError

from app.application.ports.dto.security import AccessPrincipal
from app.domain.aggregates.user import UserRole
from app.infrastructure.security.access_token_issuer import (
    PyJWTAccessTokenIssuer,
)
from app.infrastructure.security.access_token_verifier import (
    PyJWTAccessTokenVerifier,
)
from app.infrastructure.security.rsa_keys import (
    load_rsa_key_pair,
    load_rsa_public_key,
)

KEY_ID = "auth-2026-07"
ISSUER = "payflow-auth"
AUDIENCE = "order-service"


@pytest.fixture(scope="module")
def private_key() -> RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )


def create_issuer(private_key: RSAPrivateKey) -> PyJWTAccessTokenIssuer:
    return PyJWTAccessTokenIssuer(
        private_key=private_key,
        key_id=KEY_ID,
        issuer=ISSUER,
        audiences=frozenset({AUDIENCE, "orchestrator-service"}),
        access_token_ttl=timedelta(minutes=15),
    )


def create_verifier(private_key: RSAPrivateKey) -> PyJWTAccessTokenVerifier:
    return PyJWTAccessTokenVerifier(
        public_keys={KEY_ID: private_key.public_key()},
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def create_payload(now: datetime) -> dict[str, object]:
    return {
        "iss": ISSUER,
        "sub": str(uuid4()),
        "aud": [AUDIENCE],
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": str(uuid4()),
        "role": UserRole.USER.value,
    }


def encode_rs256(
    private_key: RSAPrivateKey,
    payload: dict[str, object],
    *,
    key_id: str = KEY_ID,
    token_type: str = "at+jwt",
) -> str:
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={
            "kid": key_id,
            "typ": token_type,
        },
    )


class TestPyJWTAccessTokenVerifier:
    def test_verifies_issuer_token_and_normalizes_claims(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """полный путь issuer -> verifier для access JWT."""
        now = datetime.now(UTC).replace(microsecond=0)
        principal = AccessPrincipal(user_id=uuid4(), role=UserRole.ADMIN)
        token = create_issuer(private_key).issue(principal, now)

        claims = create_verifier(private_key).verify(token)

        assert claims.issuer == ISSUER
        assert claims.user_id == principal.user_id
        assert claims.audiences == frozenset({AUDIENCE, "orchestrator-service"})
        assert claims.issued_at == now
        assert claims.expires_at == now + timedelta(minutes=15)
        assert claims.token_id.version == 7
        assert claims.role is UserRole.ADMIN

    def test_normalizes_single_audience_string(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """допустимую строковую форму JWT audience."""
        payload = create_payload(datetime.now(UTC))
        payload["aud"] = AUDIENCE
        token = encode_rs256(private_key, payload)

        claims = create_verifier(private_key).verify(token)

        assert claims.audiences == frozenset({AUDIENCE})

    @pytest.mark.parametrize(
        ("issuer", "audiences"),
        [
            ("another-issuer", [AUDIENCE]),
            (ISSUER, ["another-service"]),
        ],
    )
    def test_rejects_wrong_issuer_or_audience(
        self,
        private_key: RSAPrivateKey,
        issuer: str,
        audiences: list[str],
    ) -> None:
        """привязку токена к доверенному issuer и текущему сервису."""
        payload = create_payload(datetime.now(UTC))
        payload["iss"] = issuer
        payload["aud"] = audiences
        token = encode_rs256(private_key, payload)

        with pytest.raises(TokenMalformedError) as captured:
            create_verifier(private_key).verify(token)

        assert isinstance(captured.value, InvalidTokenError)
        assert isinstance(captured.value.__cause__, jwt.PyJWTError)

    @pytest.mark.parametrize(
        ("key_id", "token_type"),
        [
            ("unknown-key", "at+jwt"),
            (KEY_ID, "refresh+jwt"),
        ],
    )
    def test_rejects_unknown_key_or_wrong_token_type(
        self,
        private_key: RSAPrivateKey,
        key_id: str,
        token_type: str,
    ) -> None:
        """локальный выбор ключа по kid и явный тип access-токена."""
        token = encode_rs256(
            private_key,
            create_payload(datetime.now(UTC)),
            key_id=key_id,
            token_type=token_type,
        )

        with pytest.raises(TokenMalformedError) as captured:
            create_verifier(private_key).verify(token)

        assert isinstance(captured.value, InvalidTokenError)

    def test_rejects_algorithm_substitution(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """защиту от подмены жестко заданного RS256."""
        token = jwt.encode(
            create_payload(datetime.now(UTC)),
            "attacker-controlled-secret-with-enough-test-entropy",
            algorithm="HS256",
            headers={"kid": KEY_ID, "typ": "at+jwt"},
        )

        with pytest.raises(TokenMalformedError):
            create_verifier(private_key).verify(token)

    def test_rejects_signature_from_another_key(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """криптографическую подпись выбранным публичным ключом."""
        another_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        token = encode_rs256(
            another_key,
            create_payload(datetime.now(UTC)),
        )

        with pytest.raises(TokenMalformedError) as captured:
            create_verifier(private_key).verify(token)

        assert isinstance(captured.value.__cause__, jwt.InvalidSignatureError)

    def test_reports_expired_token_separately(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """истечение короткоживущего access-токена."""
        payload = create_payload(datetime.now(UTC) - timedelta(hours=1))
        token = encode_rs256(private_key, payload)

        with pytest.raises(TokenExpiredError) as captured:
            create_verifier(private_key).verify(token)

        assert isinstance(captured.value, InvalidTokenError)
        assert isinstance(captured.value.__cause__, jwt.ExpiredSignatureError)

    @pytest.mark.parametrize(
        "missing_claim",
        ["iss", "sub", "aud", "iat", "exp", "jti", "role"],
    )
    def test_requires_every_access_claim(
        self,
        private_key: RSAPrivateKey,
        missing_claim: str,
    ) -> None:
        """обязательность каждого access claim по отдельности."""
        payload = create_payload(datetime.now(UTC))
        del payload[missing_claim]
        token = encode_rs256(private_key, payload)

        with pytest.raises(TokenMalformedError) as captured:
            create_verifier(private_key).verify(token)

        assert isinstance(
            captured.value.__cause__,
            jwt.MissingRequiredClaimError,
        )

    def test_accepts_expiration_inside_clock_skew(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """ограниченный clock skew при проверке exp."""
        now = datetime.now(UTC)
        payload = create_payload(now - timedelta(minutes=10))
        payload["exp"] = now - timedelta(seconds=15)
        token = encode_rs256(private_key, payload)

        tolerant_verifier = PyJWTAccessTokenVerifier(
            public_keys={KEY_ID: private_key.public_key()},
            issuer=ISSUER,
            audience=AUDIENCE,
            leeway=timedelta(seconds=30),
        )
        strict_verifier = PyJWTAccessTokenVerifier(
            public_keys={KEY_ID: private_key.public_key()},
            issuer=ISSUER,
            audience=AUDIENCE,
            leeway=timedelta(seconds=5),
        )

        claims = tolerant_verifier.verify(token)

        assert claims.expires_at < now
        with pytest.raises(TokenExpiredError):
            strict_verifier.verify(token)

    @pytest.mark.parametrize(
        ("claim_name", "invalid_value"),
        [
            ("sub", "not-a-uuid"),
            ("jti", "not-a-uuid"),
            ("role", "SUPERUSER"),
        ],
    )
    def test_rejects_invalid_normalized_claim(
        self,
        private_key: RSAPrivateKey,
        claim_name: str,
        invalid_value: str,
    ) -> None:
        """доменную нормализацию идентификаторов и роли."""
        payload = create_payload(datetime.now(UTC))
        payload[claim_name] = invalid_value
        token = encode_rs256(private_key, payload)

        with pytest.raises(InvalidTokenDataError) as captured:
            create_verifier(private_key).verify(token)

        assert isinstance(captured.value, InvalidTokenError)
        assert isinstance(captured.value.__cause__, ValidationError)

    def test_rejects_invalid_verifier_configuration(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """fail-fast для пустого key ring verifier-а."""
        with pytest.raises(InvalidTokenConfigurationError) as captured:
            PyJWTAccessTokenVerifier(
                public_keys={},
                issuer=ISSUER,
                audience=AUDIENCE,
            )

        assert not isinstance(captured.value, InvalidTokenError)


class TestAccessTokenKeyRotation:
    def test_overlap_accepts_old_and_new_kid_then_retires_old_key(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """окно перекрытия при ротации публичных RSA-ключей."""
        new_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        now = datetime.now(UTC).replace(microsecond=0)
        principal = AccessPrincipal(user_id=uuid4(), role=UserRole.USER)
        old_issuer = PyJWTAccessTokenIssuer(
            private_key=private_key,
            key_id="auth-old",
            issuer=ISSUER,
            audiences=frozenset({AUDIENCE}),
            access_token_ttl=timedelta(minutes=15),
        )
        new_issuer = PyJWTAccessTokenIssuer(
            private_key=new_private_key,
            key_id="auth-new",
            issuer=ISSUER,
            audiences=frozenset({AUDIENCE}),
            access_token_ttl=timedelta(minutes=15),
        )
        old_token = old_issuer.issue(principal, now)
        new_token = new_issuer.issue(principal, now)

        overlap_verifier = PyJWTAccessTokenVerifier(
            public_keys={
                "auth-old": private_key.public_key(),
                "auth-new": new_private_key.public_key(),
            },
            issuer=ISSUER,
            audience=AUDIENCE,
        )

        assert overlap_verifier.verify(old_token).user_id == principal.user_id
        assert overlap_verifier.verify(new_token).user_id == principal.user_id

        # После ACCESS_TOKEN_TTL + clock skew конфигурация перезагружается уже
        # без старого ключа. Существующий verifier мутировать не требуется.
        new_only_verifier = PyJWTAccessTokenVerifier(
            public_keys={
                "auth-new": new_private_key.public_key(),
            },
            issuer=ISSUER,
            audience=AUDIENCE,
        )

        with pytest.raises(TokenMalformedError):
            new_only_verifier.verify(old_token)

        assert new_only_verifier.verify(new_token).user_id == principal.user_id


class TestRSAPublicKeyLoading:
    def test_loads_public_key_and_matching_pair(
        self,
        tmp_path,
        private_key: RSAPrivateKey,
    ) -> None:
        """загрузку публичного PEM и согласованной RSA-пары."""
        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        private_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        public_key = load_rsa_public_key(public_path)
        key_pair = load_rsa_key_pair(private_path, public_path)

        assert public_key.public_numbers() == private_key.public_key().public_numbers()
        assert key_pair.public_key.public_numbers() == public_key.public_numbers()

    def test_rejects_mismatched_key_pair(
        self,
        tmp_path,
        private_key: RSAPrivateKey,
    ) -> None:
        """согласованность закрытого и распространяемого публичного ключа."""
        another_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        private_path = tmp_path / "private.pem"
        public_path = tmp_path / "public.pem"
        private_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            another_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        with pytest.raises(InvalidTokenSigningKeyError) as captured:
            load_rsa_key_pair(private_path, public_path)

        assert not isinstance(captured.value, InvalidTokenError)
