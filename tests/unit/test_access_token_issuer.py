from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.application.ports.dto.security import AccessPrincipal
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    AccessTokenVerifierProtocol,
)
from app.core.settings import Settings
from app.domain.exceptions import (
    InvalidTokenConfigurationError,
    InvalidTokenDataError,
    InvalidTokenError,
    InvalidTokenSigningKeyError,
)
from app.domain.users import UserRole
from app.infrastructure.di import create_container
from app.infrastructure.security.access_token_issuer import (
    PyJWTAccessTokenIssuer,
)
from app.infrastructure.security.rsa_keys import load_rsa_private_key


@pytest.fixture(scope="module")
def private_key() -> RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )


def create_issuer(private_key: RSAPrivateKey) -> PyJWTAccessTokenIssuer:
    return PyJWTAccessTokenIssuer(
        private_key=private_key,
        key_id="auth-2026-07",
        issuer="payflow-auth",
        audiences=frozenset({"order-service", "orchestrator-service"}),
        access_token_ttl=timedelta(minutes=15),
    )


class TestPyJWTAccessTokenIssuer:
    def test_issues_complete_rs256_access_token(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """
        Проверяем: выпуск access JWT для аутентифицированного пользователя.
        Успех: заголовок, подпись и обязательные claims имеют ожидаемые значения.
        Нежелательное поведение: алгоритм изменен или в токен попало лишнее состояние.
        """
        issuer = create_issuer(private_key)
        principal = AccessPrincipal(user_id=uuid4(), role=UserRole.ADMIN)
        now = datetime.now(UTC).replace(microsecond=0)

        token = issuer.issue(principal, now)

        header = jwt.get_unverified_header(token)
        payload = jwt.decode(
            token,
            private_key.public_key(),
            algorithms=["RS256"],
            issuer="payflow-auth",
            audience="order-service",
            options={
                "require": [
                    "iss",
                    "sub",
                    "aud",
                    "iat",
                    "exp",
                    "jti",
                    "role",
                ]
            },
        )

        assert header == {
            "alg": "RS256",
            "kid": "auth-2026-07",
            "typ": "at+jwt",
        }
        assert payload["iss"] == "payflow-auth"
        assert payload["sub"] == str(principal.user_id)
        assert payload["aud"] == ["orchestrator-service", "order-service"]
        assert payload["iat"] == int(now.timestamp())
        assert payload["exp"] == int((now + timedelta(minutes=15)).timestamp())
        assert UUID(payload["jti"]).version == 7
        assert payload["role"] == UserRole.ADMIN.value
        assert "sid" not in payload

    def test_public_key_from_another_pair_cannot_verify_token(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """
        Проверяем: границу доверия между закрытым и публичным RSA-ключом.
        Успех: публичный ключ другой пары не подтверждает подпись access-токена.
        Нежелательное поведение: произвольный RSA-ключ принимает выпущенный токен.
        """
        token = create_issuer(private_key).issue(
            AccessPrincipal(user_id=uuid4(), role=UserRole.USER),
            datetime.now(UTC),
        )
        another_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(
                token,
                another_private_key.public_key(),
                algorithms=["RS256"],
                audience="order-service",
                issuer="payflow-auth",
            )

    def test_rejects_naive_issue_time(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """
        Проверяем: контракт времени выпуска access-токена.
        Успех: issuer отклоняет datetime без часового пояса.
        Нежелательное поведение: локальное время неявно принимается за UTC.
        """
        issuer = create_issuer(private_key)
        principal = AccessPrincipal(user_id=uuid4(), role=UserRole.USER)

        with pytest.raises(InvalidTokenDataError) as captured:
            issuer.issue(principal, datetime.now())

        assert isinstance(captured.value, InvalidTokenError)

    def test_rejects_invalid_issuer_configuration(
        self,
        private_key: RSAPrivateKey,
    ) -> None:
        """
        Проверяем: конфигурацию issuer до начала выпуска токенов.
        Успех: пустой kid сообщает отдельную доменную ошибку конфигурации.
        Нежелательное поведение: общий ValueError выходит из security-адаптера.
        """
        with pytest.raises(InvalidTokenConfigurationError) as captured:
            PyJWTAccessTokenIssuer(
                private_key=private_key,
                key_id=" ",
                issuer="payflow-auth",
                audiences=frozenset({"order-service"}),
                access_token_ttl=timedelta(minutes=15),
            )

        assert isinstance(captured.value, InvalidTokenError)


class TestRSAPrivateKeyLoading:
    def test_loads_pkcs8_pem_key_once_for_issuer(
        self,
        tmp_path,
        private_key: RSAPrivateKey,
    ) -> None:
        """
        Проверяем: загрузку закрытого RSA-ключа из PKCS8 PEM.
        Успех: загруженный ключ соответствует исходной публичной части.
        Нежелательное поведение: issuer получает непроверенную строку PEM.
        """
        private_key_path = tmp_path / "access-token.pem"
        private_key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

        loaded_key = load_rsa_private_key(private_key_path)

        assert (
            loaded_key.public_key().public_numbers()
            == private_key.public_key().public_numbers()
        )

    def test_rejects_invalid_pem_without_exposing_its_contents(
        self,
        tmp_path,
    ) -> None:
        """
        Проверяем: fail-fast при поврежденном закрытом ключе.
        Успех: загрузка завершается безопасной конфигурационной ошибкой.
        Нежелательное поведение: содержимое закрытого ключа попадает в сообщение ошибки.
        """
        private_key_path = tmp_path / "access-token.pem"
        private_key_path.write_text("sensitive-invalid-private-key")

        with pytest.raises(InvalidTokenSigningKeyError) as captured:
            load_rsa_private_key(private_key_path)

        assert isinstance(captured.value, InvalidTokenError)
        assert isinstance(captured.value.__cause__, ValueError)
        assert "sensitive-invalid-private-key" not in str(captured.value)


class TestAccessTokenIssuerDI:
    async def test_builds_app_scoped_issuer_and_verifier_from_settings(
        self,
        tmp_path,
        private_key: RSAPrivateKey,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Проверяем: сборку issuer и verifier через настройки и Dishka.
        Успех: APP scope загружает согласованную RSA-пару и проверяет свой токен.
        Нежелательное поведение: ключи читаются вручную в application-слое.
        """
        private_key_path = tmp_path / "access-token.pem"
        public_key_path = tmp_path / "access-token-public.pem"
        private_key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_key_path.write_bytes(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(private_key_path))
        monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(public_key_path))
        monkeypatch.setenv("JWT_ACTIVE_KEY_ID", "auth-test")
        monkeypatch.setenv("JWT_ISSUER", "payflow-auth")
        monkeypatch.setenv("JWT_SERVICE_AUDIENCE", "auth-service")
        monkeypatch.setenv(
            "JWT_AUDIENCES", "auth-service, order-service, orchestrator-service"
        )
        container = create_container()

        try:
            issuer = await container.get(AccessTokenIssuerProtocol)
            verifier = await container.get(AccessTokenVerifierProtocol)
            token = issuer.issue(
                AccessPrincipal(user_id=uuid4(), role=UserRole.USER),
                datetime.now(UTC),
            )
            claims = verifier.verify(token)
        finally:
            await container.close()

        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"
        assert header["kid"] == "auth-test"
        assert claims.issuer == "payflow-auth"
        assert claims.role is UserRole.USER

    def test_settings_report_invalid_audience_as_domain_error(
        self,
        tmp_path,
    ) -> None:
        """
        Проверяем: валидацию JWT audience на границе настроек.
        Успех: пустой audience сообщает доменную ошибку конфигурации токена.
        Нежелательное поведение: Pydantic ValueError протекает в startup.
        """
        with pytest.raises(InvalidTokenConfigurationError) as captured:
            Settings(
                DATABASE_HOST="localhost",
                DATABASE_PORT=5432,
                DATABASE_USER="auth",
                DATABASE_PASSWORD="auth",
                DATABASE_NAME="auth",
                DEV_LOGS=True,
                JWT_PRIVATE_KEY_PATH=tmp_path / "access-token.pem",
                JWT_PUBLIC_KEY_PATH=tmp_path / "access-token-public.pem",
                JWT_ACTIVE_KEY_ID="auth-test",
                JWT_ISSUER="payflow-auth",
                JWT_AUDIENCES=" ",
            )

        assert isinstance(captured.value, InvalidTokenError)

    def test_settings_require_local_audience_in_issued_tokens(
        self,
        tmp_path,
    ) -> None:
        """
        Проверяем: согласованность audience локального verifier-а и issuer-а.
        Успех: auth-service обязан входить в список получателей своих токенов.
        Нежелательное поведение: сервис выпускает токен, который сам отклоняет.
        """
        with pytest.raises(InvalidTokenConfigurationError) as captured:
            Settings(
                DATABASE_HOST="localhost",
                DATABASE_PORT=5432,
                DATABASE_USER="auth",
                DATABASE_PASSWORD="auth",
                DATABASE_NAME="auth",
                DEV_LOGS=True,
                JWT_PRIVATE_KEY_PATH=tmp_path / "access-token.pem",
                JWT_PUBLIC_KEY_PATH=tmp_path / "access-token-public.pem",
                JWT_ACTIVE_KEY_ID="auth-test",
                JWT_ISSUER="payflow-auth",
                JWT_SERVICE_AUDIENCE="auth-service",
                JWT_AUDIENCES="order-service",
            )

        assert isinstance(captured.value, InvalidTokenError)
