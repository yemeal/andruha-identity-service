"""
Маппинг доменных исключений на публичный HTTP-контракт.

Handler не логирует и не принимает бизнес-решений. Для нескольких внутренних
причин он может вернуть клиенту одну безопасную публичную ошибку.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    IdempotencyStorageUnavailableError,
    RefreshReplayUnavailableError,
)
from app.domain.exceptions import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTokenError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from app.entrypoints.http.schemas.errors import ApiErrorResponse, AuthApiErrorCode

_NO_STORE_HEADERS = (
    ("Cache-Control", "no-store"),
    ("Pragma", "no-cache"),
)


@dataclass(frozen=True, slots=True)
class HttpErrorSpec:
    status_code: int
    code: AuthApiErrorCode
    detail: str
    headers: tuple[tuple[str, str], ...] = _NO_STORE_HEADERS


_HTTP_ERROR_SPECS: Mapping[type[Exception], HttpErrorSpec] = MappingProxyType(
    {
        RequestValidationError: HttpErrorSpec(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=AuthApiErrorCode.VALIDATION_ERROR,
            detail="request validation failed",
        ),
        UserAlreadyExistsError: HttpErrorSpec(
            status_code=status.HTTP_409_CONFLICT,
            code=AuthApiErrorCode.EMAIL_ALREADY_EXISTS,
            detail="user with this email already exists",
        ),
        InvalidCredentialsError: HttpErrorSpec(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code=AuthApiErrorCode.INVALID_CREDENTIALS,
            detail="invalid email or password",
        ),
        InvalidTokenError: HttpErrorSpec(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code=AuthApiErrorCode.INVALID_TOKEN,
            detail="invalid or expired token",
        ),
        InvalidRefreshTokenError: HttpErrorSpec(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code=AuthApiErrorCode.INVALID_REFRESH_TOKEN,
            detail="refresh token is invalid",
        ),
        IdempotencyKeyConflictError: HttpErrorSpec(
            status_code=status.HTTP_409_CONFLICT,
            code=AuthApiErrorCode.IDEMPOTENCY_KEY_CONFLICT,
            detail="idempotency key was used with another request",
        ),
        IdempotencyRequestInProgressError: HttpErrorSpec(
            status_code=status.HTTP_423_LOCKED,
            code=AuthApiErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS,
            detail="request with this idempotency key is still processing",
        ),
        IdempotencyStorageUnavailableError: HttpErrorSpec(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=AuthApiErrorCode.IDEMPOTENCY_UNAVAILABLE,
            detail="request safety service is temporarily unavailable",
        ),
        RefreshReplayUnavailableError: HttpErrorSpec(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=AuthApiErrorCode.REFRESH_REPLAY_UNAVAILABLE,
            detail="refresh replay is temporarily unavailable",
        ),
        UserNotFoundError: HttpErrorSpec(
            status_code=status.HTTP_404_NOT_FOUND,
            code=AuthApiErrorCode.USER_NOT_FOUND,
            detail="user not found",
        ),
    }
)

_INTERNAL_ERROR_SPEC = HttpErrorSpec(
    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    code=AuthApiErrorCode.INTERNAL_ERROR,
    detail="internal server error",
)


ExceptionHandler = Callable[[Request, Exception], Awaitable[Response]]


def openapi_error_responses(
    *error_types: type[Exception],
) -> dict[int | str, dict[str, Any]]:
    """
    Построить OpenAPI responses из того же контракта, что использует runtime.

    Multiple internal errors with one status are separate examples of the same
    stable ApiErrorResponse schema.
    """
    responses: dict[int | str, dict[str, Any]] = {}

    for error_type in error_types:
        spec = _HTTP_ERROR_SPECS[error_type]
        response = responses.setdefault(
            spec.status_code,
            {
                "model": ApiErrorResponse,
                "description": "Request rejected",
                "headers": {
                    "Cache-Control": {
                        "description": "Prevents caching authentication responses",
                        "schema": {"type": "string", "example": "no-store"},
                    },
                    "Pragma": {
                        "description": "Legacy cache prevention",
                        "schema": {"type": "string", "example": "no-cache"},
                    },
                },
                "content": {
                    "application/json": {
                        "examples": {},
                    }
                },
            },
        )
        content = cast(dict[str, Any], response["content"])
        media_type = cast(dict[str, Any], content["application/json"])
        examples = cast(dict[str, Any], media_type["examples"])
        examples[spec.code.value] = {
            "summary": spec.detail,
            "value": {
                "code": spec.code.value,
                "detail": spec.detail,
            },
        }

        if error_type is IdempotencyRequestInProgressError:
            headers = cast(dict[str, Any], response["headers"])
            headers["Retry-After"] = {
                "description": "Seconds before retrying the same request",
                "schema": {"type": "integer", "minimum": 1},
            }

    return responses


def _response_from_spec(spec: HttpErrorSpec) -> JSONResponse:
    response = ApiErrorResponse(
        code=spec.code,
        detail=spec.detail,
    )
    return JSONResponse(
        status_code=spec.status_code,
        content=response.model_dump(mode="json"),
        headers=dict(spec.headers),
    )


def create_internal_error_response() -> Response:
    """Создать Auth-specific safe 500 для outer ASGI middleware."""
    return _response_from_spec(_INTERNAL_ERROR_SPEC)


def create_error_response(error_type: type[Exception]) -> JSONResponse:
    """Build the stable public response for a known application/domain error."""
    return _response_from_spec(_HTTP_ERROR_SPECS[error_type])


def _create_exception_handler(spec: HttpErrorSpec) -> ExceptionHandler:
    async def handler(_request: Request, _error: Exception) -> Response:
        return _response_from_spec(spec)

    return handler


def register_exception_handlers(
    app: FastAPI,
    *,
    idempotency_retry_after_seconds: int = 1,
) -> None:
    for error_type, spec in _HTTP_ERROR_SPECS.items():
        if error_type is IdempotencyRequestInProgressError:
            spec = HttpErrorSpec(
                status_code=spec.status_code,
                code=spec.code,
                detail=spec.detail,
                headers=(
                    *spec.headers,
                    (
                        "Retry-After",
                        str(idempotency_retry_after_seconds),
                    ),
                ),
            )
        app.add_exception_handler(error_type, _create_exception_handler(spec))
