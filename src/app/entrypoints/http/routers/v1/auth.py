from typing import Annotated, Any

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Cookie, Header, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import (
    APIKeyCookie,
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from app.application.dto.token_pair import TokenPair
from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    IdempotencyStorageUnavailableError,
    RefreshReplayUnavailableError,
)
from app.application.exceptions.profiles import ProfileProvisioningUnavailableError
from app.application.exceptions.security import InvalidTokenError
from app.application.idempotency.fingerprint import hash_idempotency_key
from app.application.ports.dto.registration import RegistrationOutcome
from app.application.use_cases.get_current_user.handler import GetCurrentUserHandler
from app.application.use_cases.get_current_user.query import GetCurrentUserQuery
from app.application.use_cases.login.command import LoginCommand
from app.application.use_cases.login.handler import LoginHandler
from app.application.use_cases.logout.command import LogoutCommand
from app.application.use_cases.logout.handler import LogoutHandler
from app.application.use_cases.refresh.command import RefreshCommand
from app.application.use_cases.refresh.handler import RefreshHandler
from app.application.use_cases.register.command import RegisterUserCommand
from app.application.use_cases.register.handler import RegisterUserHandler
from app.core.settings import SecuritySettings
from app.domain.exceptions import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    UserAlreadyExistsError,
)
from app.entrypoints.http.routers.exception_handlers import (
    create_error_response,
    openapi_error_responses,
)
from app.entrypoints.http.schemas.auth import (
    LoginRequest,
    MeResponse,
    PendingRegistrationResponse,
    RegisterRequest,
    RegisterResponse,
    TestLoginResponse,
)


def _cookie_response(description: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI collapses Set-Cookie headers, so describe both cookies explicitly."""
    return {
        status.HTTP_204_NO_CONTENT: {
            "description": description,
            "headers": {
                "Set-Cookie": {
                    "description": (
                        "Two Set-Cookie fields for access_token and "
                        "refresh_token HttpOnly cookies"
                    ),
                    "schema": {"type": "string"},
                },
                "Cache-Control": {
                    "description": "Always no-store",
                    "schema": {"type": "string", "example": "no-store"},
                },
            },
        }
    }


def _set_no_store_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _set_tokens_cookies(
    response: Response,
    pair: TokenPair,
    settings: SecuritySettings,
) -> None:
    response.set_cookie(
        key="access_token",
        value=pair.access_token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        max_age=settings.ACCESS_TOKEN_TTL_SECONDS,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=pair.refresh_token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        max_age=settings.AUTH_SESSION_IDLE_TTL_SECONDS,
        path="/api/v1/auth",
    )
    _set_no_store_headers(response)


def _delete_auth_cookies(response: Response, settings: SecuritySettings) -> None:
    response.delete_cookie(
        key="access_token",
        path="/",
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    response.delete_cookie(
        key="refresh_token",
        path="/api/v1/auth",
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    _set_no_store_headers(response)


def create_auth_router(
    *,
    include_test_token_endpoint: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])
    internal_access_bearer = HTTPBearer(
        scheme_name="InternalAccessBearer",
        description="Bearer access token injected by the trusted API Gateway",
        auto_error=False,
    )
    refresh_cookie = APIKeyCookie(
        name="refresh_token",
        scheme_name="RefreshTokenCookie",
        description="HttpOnly refresh-token cookie scoped to /api/v1/auth",
        auto_error=False,
    )

    @router.post(
        "/register",
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_202_ACCEPTED: {
                "model": PendingRegistrationResponse,
                "description": (
                    "Registration is durable and awaiting profile reconciliation"
                ),
                "headers": {
                    "Retry-After": {
                        "description": "Seconds before retrying with the same key",
                        "schema": {"type": "integer", "minimum": 1},
                    }
                },
            },
            **openapi_error_responses(
                UserAlreadyExistsError,
                ProfileProvisioningUnavailableError,
                IdempotencyKeyConflictError,
                RequestValidationError,
            ),
        },
    )
    @inject
    async def register(
        payload: RegisterRequest,
        response: Response,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=8,
                max_length=128,
            ),
        ],
        registration: FromDishka[RegisterUserHandler],
    ) -> RegisterResponse | PendingRegistrationResponse:
        result = await registration.execute(
            RegisterUserCommand(
                email=payload.email,
                password=payload.password,
                key_hash=hash_idempotency_key(idempotency_key),
            )
        )
        _set_no_store_headers(response)
        if result.outcome is RegistrationOutcome.PENDING:
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = str(result.retry_after_seconds or 1)
            return PendingRegistrationResponse(
                registration_id=result.operation_id,
                user_id=result.user_id,
            )
        return RegisterResponse(user_id=result.user_id)

    @router.post(
        "/login",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses={
            **_cookie_response(
                "Authentication succeeded; tokens are set only as cookies"
            ),
            **openapi_error_responses(
                InvalidCredentialsError,
                RequestValidationError,
            ),
        },
    )
    @inject
    async def login(
        payload: LoginRequest,
        handler: FromDishka[LoginHandler],
        settings: FromDishka[SecuritySettings],
    ) -> Response:
        pair = await handler.execute(
            LoginCommand(email=payload.email, password=payload.password)
        )

        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _set_tokens_cookies(response, pair, settings)
        return response

    if include_test_token_endpoint:

        @router.post(
            "/login/test",
            response_model=TestLoginResponse,
            responses=openapi_error_responses(
                InvalidCredentialsError,
                RequestValidationError,
            ),
        )
        @inject
        async def login_test(
            payload: LoginRequest,
            response: Response,
            handler: FromDishka[LoginHandler],
        ) -> TestLoginResponse:
            """Return tokens for local integration clients; disabled in production."""
            pair = await handler.execute(
                LoginCommand(email=payload.email, password=payload.password)
            )
            _set_no_store_headers(response)
            return TestLoginResponse.model_validate(pair.model_dump(mode="python"))

    @router.post(
        "/refresh",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses={
            **_cookie_response(
                "Token rotation succeeded; replacement tokens are set only as cookies"
            ),
            **openapi_error_responses(
                InvalidRefreshTokenError,
                RequestValidationError,
                IdempotencyKeyConflictError,
                IdempotencyRequestInProgressError,
                IdempotencyStorageUnavailableError,
                RefreshReplayUnavailableError,
            ),
        },
    )
    @inject
    async def refresh(
        refresh_token: Annotated[
            str | None,
            Security(refresh_cookie),
        ],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=8,
                max_length=128,
            ),
        ],
        refresh_use_case: FromDishka[RefreshHandler],
        settings: FromDishka[SecuritySettings],
    ) -> Response:
        if refresh_token is None:
            raise InvalidRefreshTokenError()

        try:
            pair = await refresh_use_case.execute(
                RefreshCommand(
                    refresh_token=refresh_token,
                    key_hash=hash_idempotency_key(idempotency_key.strip()),
                )
            )
        except InvalidRefreshTokenError:
            response = create_error_response(InvalidRefreshTokenError)
            _delete_auth_cookies(response, settings)
            return response

        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _set_tokens_cookies(response, pair, settings)
        return response

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses=_cookie_response(
            "Logout is idempotent; both authentication cookies are deleted"
        ),
    )
    @inject
    async def logout(
        handler: FromDishka[LogoutHandler],
        settings: FromDishka[SecuritySettings],
        refresh_token: Annotated[
            str | None,
            Cookie(alias="refresh_token"),
        ] = None,
    ) -> Response:
        await handler.execute(LogoutCommand(refresh_token=refresh_token))

        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _delete_auth_cookies(response, settings)
        return response

    @router.get(
        "/me",
        responses=openapi_error_responses(InvalidTokenError),
    )
    @inject
    async def me(
        response: Response,
        handler: FromDishka[GetCurrentUserHandler],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(internal_access_bearer),
        ] = None,
    ) -> MeResponse:
        if credentials is None:
            raise InvalidTokenError()

        user = await handler.execute(
            GetCurrentUserQuery(access_token=credentials.credentials)
        )
        _set_no_store_headers(response)
        return MeResponse.model_validate(user)

    return router
