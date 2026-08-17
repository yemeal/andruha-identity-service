from app.domain.exceptions.base import DomainError


class AuthError(DomainError):
    """Базовая ошибка домена аутентификации."""


class InvalidCredentialsError(AuthError):
    """Учетные данные не прошли проверку."""

    default_message = "Incorrect email or password"


class UserError(AuthError):
    """Базовая ошибка пользователя в контексте аутентификации."""


class UserAlreadyExistsError(UserError):
    """A user with the normalized email already exists."""

    default_message = "User with this email already exists"


class UserNotFoundError(UserError):
    """Пользователь не найден."""

    default_message = "User not found"


class TokenError(AuthError):
    """Базовая ошибка выпуска и проверки auth-токенов."""


class InvalidTokenError(TokenError):
    """Base error for invalid token data or issuance."""

    default_message = "Token is invalid"


class InvalidTokenConfigurationError(InvalidTokenError):
    """Настройки выпуска или проверки токена противоречат контракту."""

    default_message = "Token configuration is invalid"


class InvalidTokenSigningKeyError(InvalidTokenError):
    """Ключ подписи отсутствует, поврежден или имеет неподходящий тип."""

    default_message = "Token signing key is invalid"


class InvalidTokenDataError(InvalidTokenError):
    """Данные токена или временные значения противоречат контракту."""

    default_message = "Token data is invalid"


class TokenExpiredError(InvalidTokenError):
    """Срок действия токена истек."""

    default_message = "Token has expired"


class TokenMalformedError(InvalidTokenError):
    """Токен нельзя декодировать или проверить."""

    default_message = "Token is malformed"


class InvalidRefreshTokenError(InvalidTokenError):
    """Refresh-токен нельзя использовать для продолжения сессии."""

    default_message = "Refresh token is invalid"


class SessionError(InvalidRefreshTokenError):
    """Базовая ошибка сессии, обнаруженная при проверке refresh-токена."""


class AuthSessionInactiveError(SessionError):
    """The refresh-token session expired or was revoked."""

    default_message = "Auth session is inactive"


class RefreshTokenReuseError(SessionError):
    """Уже использованный refresh-токен предъявлен повторно."""

    default_message = "Refresh token reuse detected"


class _AuthErrors:
    Error = AuthError
    INVALID_CREDENTIALS = InvalidCredentialsError


class _UserErrors:
    Error = UserError
    EMAIL_ALREADY_EXISTS = UserAlreadyExistsError
    NOT_FOUND = UserNotFoundError


class _TokenErrors:
    Error = TokenError
    INVALID = InvalidTokenError
    INVALID_CONFIGURATION = InvalidTokenConfigurationError
    INVALID_SIGNING_KEY = InvalidTokenSigningKeyError
    INVALID_DATA = InvalidTokenDataError
    EXPIRED = TokenExpiredError
    MALFORMED = TokenMalformedError
    INVALID_REFRESH = InvalidRefreshTokenError


class _SessionErrors:
    Error = SessionError
    INACTIVE = AuthSessionInactiveError
    REFRESH_TOKEN_REUSED = RefreshTokenReuseError


class DomainErrors:
    """Сгруппированные типы ошибок домена auth_service."""

    Auth = _AuthErrors
    User = _UserErrors
    Token = _TokenErrors
    Session = _SessionErrors
