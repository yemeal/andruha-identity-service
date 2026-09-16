from app.domain.exceptions import InvalidPasswordError


def validate_registration_password(password: str) -> str:
    if not 8 <= len(password) <= 128:
        raise InvalidPasswordError()
    return password
