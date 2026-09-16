from app.domain.exceptions.base import DomainError


class InvalidDomainStateError(DomainError):
    default_message = "Domain state is invalid"


class InvalidEmailError(InvalidDomainStateError):
    default_message = "Email is invalid"


class InvalidPasswordHashError(InvalidDomainStateError):
    default_message = "Password hash is invalid"


class InvalidPasswordError(InvalidDomainStateError):
    default_message = "Password does not meet registration requirements"


class InvalidTimestampError(InvalidDomainStateError):
    default_message = "Domain timestamps are inconsistent"


class InvalidSessionLifetimeError(InvalidDomainStateError):
    default_message = "Session lifetime must be positive"


class InvalidCredentialsError(DomainError):
    default_message = "Incorrect email or password"


class UserAlreadyExistsError(DomainError):
    default_message = "User with this email already exists"


class UserNotFoundError(DomainError):
    default_message = "User not found"


class InvalidRefreshTokenError(DomainError):
    default_message = "Refresh token is invalid"


class AuthSessionInactiveError(InvalidRefreshTokenError):
    default_message = "Auth session is inactive"


class RefreshTokenReuseError(InvalidRefreshTokenError):
    default_message = "Refresh token reuse detected"
