from contextlib import suppress
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

from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    IdempotencyStorageUnavailableError,
    RefreshReplayUnavailableError,
)
from app.application.services.auth_service import AuthServiceProtocol, TokenPair
from app.application.services.idempotency_fingerprint import hash_idempotency_key
from app.application.services.refresh import RefreshUseCaseProtocol
from app.core.settings import SecuritySettings
from app.domain.exceptions import (
    DomainErrors,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTokenError,
    UserAlreadyExistsError,
)
from app.entrypoints.http.routers.exception_handlers import (
    create_error_response,
    openapi_error_responses,
)
from app.entrypoints.http.schemas.auth import (
    LoginRequest,
    MeResponse,
    RegisterRequest,
    RegisterResponse,
    TestLoginResponse,
)


def _cookie_response(description: str) -> dict[int | str, dict[str, Any]]:
    """
    cookie-only ответ без ложной JSON-схемы.

    OpenAPI объединяет повторяющиеся Set-Cookie в одно имя заголовка, поэтому
    точное количество cookie фиксируется в description
    """
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
        responses=openapi_error_responses(
            UserAlreadyExistsError,
            RequestValidationError,
        ),
    )
    @inject
    async def register(  # pyright: ignore[reportUnusedFunction]
        payload: RegisterRequest,
        auth_service: FromDishka[AuthServiceProtocol],
    ) -> RegisterResponse:
        user = await auth_service.register(
            email=payload.email, password=payload.password
        )
        return RegisterResponse(user_id=user.id)

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
    async def login(  # pyright: ignore[reportUnusedFunction]
        payload: LoginRequest,
        auth_service: FromDishka[AuthServiceProtocol],
        settings: FromDishka[SecuritySettings],
    ) -> Response:
        pair = await auth_service.login(email=payload.email, password=payload.password)

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
        async def login_test(  # pyright: ignore[reportUnusedFunction]
            payload: LoginRequest,
            response: Response,
            auth_service: FromDishka[AuthServiceProtocol],
        ) -> TestLoginResponse:
            """
            Вернуть токены только локальному integration test client.

            Startup configuration is fail-closed and the gateway blocks this
            exact path.
            """
            pair = await auth_service.login(
                email=payload.email,
                password=payload.password,
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
    async def refresh(  # pyright: ignore[reportUnusedFunction]
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
        refresh_use_case: FromDishka[RefreshUseCaseProtocol],
        settings: FromDishka[SecuritySettings],
    ) -> Response:
        """
        HTTP-граница делает одноразовую rotation безопасной для сетевых ретраев.

        AuthService ничего не знает про Idempotency-Key: guard либо возвращает
        готовую пару, либо разрешает ровно один вызов refresh use case.
        """
        if refresh_token is None:
            raise DomainErrors.Token.INVALID_REFRESH()

        try:
            pair = await refresh_use_case.execute(
                refresh_token=refresh_token,
                key_hash=hash_idempotency_key(idempotency_key.strip()),
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
    async def logout(  # pyright: ignore[reportUnusedFunction]
        auth_service: FromDishka[AuthServiceProtocol],
        settings: FromDishka[SecuritySettings],
        refresh_token: Annotated[
            str | None,
            Cookie(alias="refresh_token"),
        ] = None,
    ) -> Response:
        if refresh_token is not None:
            with suppress(InvalidRefreshTokenError):
                await auth_service.logout(refresh_token)

        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _delete_auth_cookies(response, settings)
        return response

    @router.get(
        "/me",
        responses=openapi_error_responses(InvalidTokenError),
    )
    @inject
    async def me(  # pyright: ignore[reportUnusedFunction]
        response: Response,
        auth_service: FromDishka[AuthServiceProtocol],
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(internal_access_bearer),
        ] = None,
    ) -> MeResponse:
        if credentials is None:
            raise DomainErrors.Token.INVALID()

        user = await auth_service.get_current_user(str(credentials.credentials))
        _set_no_store_headers(response)
        return MeResponse.model_validate(user)

    return router
