import pytest

from app.application.exceptions.security import (
    InvalidTokenConfigurationError,
    InvalidTokenSigningKeyError,
    InvalidTokenError,
    InvalidTokenDataError,
    TokenExpiredError,
    TokenMalformedError,
    TokenIssuanceError,
)
from app.domain.exceptions import (
    DomainError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RefreshTokenReuseError,
)


@pytest.mark.parametrize(
    "error_type",
    [InvalidTokenConfigurationError, InvalidTokenSigningKeyError, TokenIssuanceError],
)
def test_service_failure_is_not_an_invalid_client_token(error_type) -> None:
    assert not issubclass(error_type, InvalidTokenError)
    assert not issubclass(error_type, DomainError)


@pytest.mark.parametrize(
    "error_type", [InvalidTokenDataError, TokenExpiredError, TokenMalformedError]
)
def test_rejected_access_token_has_application_contract(error_type) -> None:
    assert issubclass(error_type, InvalidTokenError)
    assert not issubclass(error_type, DomainError)


def test_refresh_reuse_has_domain_contract() -> None:
    assert issubclass(RefreshTokenReuseError, InvalidRefreshTokenError)
    assert issubclass(InvalidRefreshTokenError, DomainError)


def test_domain_error_message_cannot_contain_caller_payload() -> None:
    with pytest.raises(TypeError):
        InvalidCredentialsError("secret")


def test_exception_chaining_preserves_cause_without_changing_public_message() -> None:
    cause = ValueError("secret")
    with pytest.raises(TokenMalformedError) as captured:
        try:
            raise cause
        except ValueError as error:
            raise TokenMalformedError() from error
    assert captured.value.__cause__ is cause
    assert "secret" not in str(captured.value)
