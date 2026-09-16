class SecurityError(Exception):
    default_message = "Security operation failed"

    def __init__(self) -> None:
        super().__init__(self.default_message)


class InvalidTokenError(SecurityError):
    default_message = "Token is invalid"


class InvalidTokenDataError(InvalidTokenError):
    default_message = "Token data is invalid"


class TokenExpiredError(InvalidTokenError):
    default_message = "Token has expired"


class TokenMalformedError(InvalidTokenError):
    default_message = "Token is malformed"


class TokenIssuanceError(SecurityError):
    default_message = "Token issuance is unavailable"


class InvalidTokenConfigurationError(SecurityError, RuntimeError):
    default_message = "Token configuration is invalid"


class InvalidTokenSigningKeyError(InvalidTokenConfigurationError):
    default_message = "Token signing key is invalid"


class PasswordHashingError(SecurityError):
    default_message = "Password hashing is unavailable"
