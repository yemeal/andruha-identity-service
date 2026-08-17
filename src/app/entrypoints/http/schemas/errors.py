from enum import StrEnum, unique

from app.entrypoints.http.schemas.base import CamelCaseBase


@unique
class AuthApiErrorCode(StrEnum):
    VALIDATION_ERROR = "request.validation_error"
    INTERNAL_ERROR = "request.internal_error"
    EMAIL_ALREADY_EXISTS = "auth.email_already_exists"
    INVALID_CREDENTIALS = "auth.invalid_credentials"
    INVALID_TOKEN = "auth.invalid_token"
    INVALID_REFRESH_TOKEN = "auth.invalid_refresh_token"
    IDEMPOTENCY_KEY_CONFLICT = "auth.idempotency_key_conflict"
    IDEMPOTENCY_REQUEST_IN_PROGRESS = "auth.idempotency_request_in_progress"
    IDEMPOTENCY_UNAVAILABLE = "auth.idempotency_unavailable"
    REFRESH_REPLAY_UNAVAILABLE = "auth.refresh_replay_unavailable"
    USER_NOT_FOUND = "auth.user_not_found"


class ApiErrorResponse(CamelCaseBase):
    """Стабильное публичное представление ошибки Auth API."""

    code: AuthApiErrorCode
    detail: str
