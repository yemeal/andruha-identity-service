from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.ports.dto.security import (
    AccessTokenClaims,
    IssuedRefreshToken,
)
from app.domain.exceptions import (
    DomainErrors,
    InvalidRefreshTokenError,
    InvalidTokenConfigurationError,
    InvalidTokenDataError,
    InvalidTokenError,
    InvalidTokenSigningKeyError,
    RefreshTokenReuseError,
    TokenMalformedError,
    UserAlreadyExistsError,
)
from app.domain.refresh_tokens import RefreshToken
from app.domain.users import UserRole


class TestDomainErrorContract:
    def test_namespace_aliases_concrete_error_type(self) -> None:
        """
        Проверяем: устройство namespace для конфликта email.
        Успех: имя в DomainErrors ссылается прямо на конкретный класс.
        Нежелательное поведение: отдельная фабрика дублирует тип исключения.
        """
        error = DomainErrors.User.EMAIL_ALREADY_EXISTS()

        assert DomainErrors.User.EMAIL_ALREADY_EXISTS is UserAlreadyExistsError
        assert isinstance(error, UserAlreadyExistsError)
        assert str(error) == "User with this email already exists"

    def test_uses_standard_exception_chaining(self) -> None:
        """
        Проверяем: адаптацию библиотечной ошибки через raise from.
        Успех: traceback хранит исходную причину отдельно от доменной ошибки.
        Нежелательное поведение: причина теряется или копируется в сообщение.
        """
        cause = ValueError("raw token must not appear in domain message")

        with pytest.raises(TokenMalformedError) as captured:
            try:
                raise cause
            except ValueError as error:
                raise DomainErrors.Token.MALFORMED() from error

        domain_error = captured.value

        assert isinstance(domain_error, InvalidTokenError)
        assert domain_error.__cause__ is cause
        assert "raw token" not in str(domain_error)

    def test_message_cannot_be_overridden_at_raise_site(self) -> None:
        """
        Проверяем: попытку передать произвольное сообщение конкретной ошибке.
        Успех: тип принимает только пустой конструктор и сохраняет единый текст.
        Нежелательное поведение: сообщения ошибок начинают зависеть от call site.
        """
        with pytest.raises(TypeError):
            DomainErrors.Auth.INVALID_CREDENTIALS("custom message")

    @pytest.mark.parametrize(
        ("namespace_type", "error_type"),
        [
            (
                DomainErrors.Token.INVALID_CONFIGURATION,
                InvalidTokenConfigurationError,
            ),
            (
                DomainErrors.Token.INVALID_SIGNING_KEY,
                InvalidTokenSigningKeyError,
            ),
            (
                DomainErrors.Token.INVALID_DATA,
                InvalidTokenDataError,
            ),
        ],
    )
    def test_token_errors_share_invalid_token_base(
        self,
        namespace_type: type[InvalidTokenError],
        error_type: type[InvalidTokenError],
    ) -> None:
        """
        Проверяем: иерархию ошибок конфигурации, ключа и данных токена.
        Успех: каждая конкретная причина является InvalidTokenError.
        Нежелательное поведение: security-слой требует отдельной обработки ValueError.
        """
        assert namespace_type is error_type
        assert issubclass(error_type, InvalidTokenError)


class TestRefreshTokenErrors:
    def test_reuse_raises_marker_error_without_logging_payload(self) -> None:
        """
        Проверяем: повторное потребление refresh-токена.
        Успех: доменная модель сообщает маркерную ошибку без logging context.
        Нежелательное поведение: доменная ошибка становится контейнером лог-данных.
        """
        now = datetime.now(UTC)
        sensitive_digest = b"sensitive-token-hash".ljust(32, b"\0")
        token = RefreshToken(
            session_id=uuid4(),
            token_hash=sensitive_digest,
            used_at=now,
        )

        with pytest.raises(RefreshTokenReuseError) as captured:
            token.consume(now)

        assert isinstance(captured.value, InvalidRefreshTokenError)
        assert isinstance(captured.value, InvalidTokenError)
        assert not hasattr(captured.value, "context")
        assert "sensitive-token-hash" not in str(captured.value)


class TestSecurityDTOErrors:
    def test_access_claims_report_invalid_time_as_domain_error(self) -> None:
        """
        Проверяем: временной контракт нормализованных access claims.
        Успех: exp раньше iat сообщает InvalidTokenDataError.
        Нежелательное поведение: DTO выбрасывает общий ValueError.
        """
        issued_at = datetime.now(UTC)

        with pytest.raises(InvalidTokenDataError) as captured:
            AccessTokenClaims(
                issuer="payflow-auth",
                user_id=uuid4(),
                audiences=frozenset({"order-service"}),
                issued_at=issued_at,
                expires_at=issued_at - timedelta(seconds=1),
                token_id=uuid4(),
                role=UserRole.USER,
            )

        assert isinstance(captured.value, InvalidTokenError)

    def test_refresh_dto_reports_invalid_digest_as_domain_error(self) -> None:
        """
        Проверяем: длину SHA-256 digest выпущенного refresh-токена.
        Успех: digest неправильной длины сообщает InvalidTokenDataError.
        Нежелательное поведение: DTO выбрасывает общий ValueError.
        """
        with pytest.raises(InvalidTokenDataError) as captured:
            IssuedRefreshToken(value="opaque-token", digest=b"too-short")

        assert isinstance(captured.value, InvalidTokenError)
