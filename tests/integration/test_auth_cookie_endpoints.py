from app.domain.exceptions import (
    UserAlreadyExistsError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
)
from app.application.exceptions.security import (
    TokenExpiredError,
    InvalidTokenConfigurationError,
)
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    IdempotencyStorageUnavailableError,
    RefreshReplayUnavailableError,
)
from app.application.ports.dto.registration import (
    RegistrationOutcome,
    RegistrationResult,
)
from app.application.dto.token_pair import TokenPair
from app.application.use_cases.login.handler import LoginHandler
from app.application.use_cases.login.command import LoginCommand
from app.application.use_cases.logout.handler import LogoutHandler
from app.application.use_cases.logout.command import LogoutCommand
from app.application.use_cases.get_current_user.handler import GetCurrentUserHandler
from app.application.use_cases.get_current_user.query import GetCurrentUserQuery
from app.application.use_cases.refresh.command import RefreshCommand
from app.application.use_cases.register.command import RegisterUserCommand
from app.application.idempotency.fingerprint import hash_idempotency_key
from app.application.use_cases.register.handler import RegisterUserHandler
from app.application.use_cases.refresh.handler import RefreshHandler
from app.core.settings import SecuritySettings, Settings
from app.domain.aggregates.user import User
from app.entrypoints.http.routers import create_api_router
from app.entrypoints.http.routers.exception_handlers import (
    register_exception_handlers,
)


class _AuthEndpointProvider(Provider):
    def __init__(
        self,
        auth_service,
        refresh_use_case,
        registration_use_case,
        settings: Settings,
    ) -> None:
        super().__init__()
        self._auth_service = auth_service
        self._refresh_use_case = refresh_use_case
        self._registration_use_case = registration_use_case
        self._settings = settings

    @provide(scope=Scope.REQUEST)
    def get_login(self) -> LoginHandler:
        return self._auth_service.login

    @provide(scope=Scope.REQUEST)
    def get_logout(self) -> LogoutHandler:
        return self._auth_service.logout

    @provide(scope=Scope.REQUEST)
    def get_current_user(self) -> GetCurrentUserHandler:
        return self._auth_service.get_current_user

    @provide(scope=Scope.REQUEST)
    def get_refresh_use_case(self) -> RefreshHandler:
        return self._refresh_use_case

    @provide(scope=Scope.REQUEST)
    def get_registration_use_case(self) -> RegisterUserHandler:
        return self._registration_use_case

    @provide(scope=Scope.APP)
    def get_settings(self) -> Settings:
        return self._settings

    @provide(scope=Scope.APP)
    def get_security_settings(self) -> SecuritySettings:
        return getattr(self._settings, "security", self._settings)


@asynccontextmanager
async def _auth_http_context(
    *,
    include_test_token_endpoint: bool,
):
    auth_service = SimpleNamespace(
        login=AsyncMock(spec=LoginHandler),
        logout=AsyncMock(spec=LogoutHandler),
        get_current_user=AsyncMock(spec=GetCurrentUserHandler),
    )
    refresh_use_case = AsyncMock(spec=RefreshHandler)
    registration_use_case = AsyncMock(spec=RegisterUserHandler)
    settings = cast(
        Settings,
        SimpleNamespace(
            ACCESS_TOKEN_TTL_SECONDS=900,
            AUTH_SESSION_IDLE_TTL_SECONDS=2_592_000,
            AUTH_COOKIE_SECURE=True,
            AUTH_COOKIE_SAMESITE="lax",
        ),
    )
    app = FastAPI()
    app.state.refresh_use_case = refresh_use_case
    app.state.registration_use_case = registration_use_case
    app.include_router(
        create_api_router(
            include_test_token_endpoint=include_test_token_endpoint,
        )
    )
    register_exception_handlers(app)
    container = make_async_container(
        _AuthEndpointProvider(
            auth_service,
            refresh_use_case,
            registration_use_case,
            settings,
        )
    )
    setup_dishka(container, app)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
        ) as client:
            yield client, auth_service, app
    finally:
        await container.close()


@pytest_asyncio.fixture
async def auth_http():
    async with _auth_http_context(
        include_test_token_endpoint=False,
    ) as context:
        yield context


@pytest_asyncio.fixture
async def auth_http_with_test_token_endpoint():
    async with _auth_http_context(
        include_test_token_endpoint=True,
    ) as context:
        yield context


def _cookie_headers(response) -> dict[str, str]:
    return {
        header.split("=", 1)[0]: header
        for header in response.headers.get_list("set-cookie")
    }


class TestRegisterEndpoint:
    async def test_returns_only_public_user_id(self, auth_http) -> None:
        """регистрация возвращает публичный идентификатор без токенов."""
        client, _auth_service, _app = auth_http
        registration = _app.state.registration_use_case
        user = User(
            email="user@example.com",
            password_hash="stored-hash",
        )
        registration.execute.return_value = RegistrationResult(
            operation_id=user.id,
            user_id=user.id,
            outcome=RegistrationOutcome.COMPLETED,
        )

        response = await client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-key-123"},
            json={
                "email": "USER@EXAMPLE.COM",
                "password": "strong-password",
            },
        )

        assert response.status_code == 201
        assert response.json() == {"userId": str(user.id)}
        assert "set-cookie" not in response.headers
        registration.execute.assert_awaited_once_with(
            RegisterUserCommand(
                email="user@example.com",
                password="strong-password",
                key_hash=hash_idempotency_key("register-key-123"),
            )
        )

    async def test_pending_registration_returns_202_and_retry_after(
        self, auth_http
    ) -> None:
        client, _auth_service, app = auth_http
        registration = app.state.registration_use_case
        operation_id = User(
            email="pending@example.com",
            password_hash="stored-hash",
        ).id
        user_id = User(
            email="reserved@example.com",
            password_hash="stored-hash",
        ).id
        registration.execute.return_value = RegistrationResult(
            operation_id=operation_id,
            user_id=user_id,
            outcome=RegistrationOutcome.PENDING,
        )

        response = await client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-key-123"},
            json={
                "email": "pending@example.com",
                "password": "strong-password",
            },
        )

        assert response.status_code == 202
        assert response.headers["Retry-After"] == "1"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "registrationId": str(operation_id),
            "userId": str(user_id),
            "status": "PENDING",
        }

    async def test_registration_requires_idempotency_key(self, auth_http) -> None:
        client, _auth_service, app = auth_http

        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "user@example.com",
                "password": "strong-password",
            },
        )

        assert response.status_code == 422
        app.state.registration_use_case.execute.assert_not_awaited()

    async def test_duplicate_email_uses_public_conflict(self, auth_http) -> None:
        """доменный конфликт регистрации проходит через общий handler."""
        client, _auth_service, _app = auth_http
        registration = _app.state.registration_use_case
        registration.execute.side_effect = UserAlreadyExistsError()

        response = await client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-key-123"},
            json={
                "email": "user@example.com",
                "password": "strong-password",
            },
        )

        assert response.status_code == 409
        assert response.json()["code"] == "auth.email_already_exists"
        assert "set-cookie" not in response.headers

    async def test_validation_error_does_not_echo_password(
        self,
        auth_http,
    ) -> None:
        """FastAPI отклоняет невалидный registration body."""
        client, _auth_service, _app = auth_http
        registration = _app.state.registration_use_case
        password = "secret-that-must-not-be-echoed"

        response = await client.post(
            "/api/v1/auth/register",
            headers={"Idempotency-Key": "register-key-123"},
            json={
                "email": "not-an-email",
                "password": password,
            },
        )

        assert response.status_code == 422
        assert response.json() == {
            "code": "request.validation_error",
            "detail": "request validation failed",
        }
        assert password not in response.text
        registration.execute.assert_not_awaited()


class TestLoginCookieEndpoint:
    async def test_sets_hardened_cookies_and_returns_no_body(
        self,
        auth_http,
    ) -> None:
        """успешный login передает оба токена только через cookie."""
        client, auth_service, _app = auth_http
        auth_service.login.execute.return_value = TokenPair(
            access_token="access-secret",
            refresh_token="refresh-secret",
        )

        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "USER@EXAMPLE.COM",
                "password": "plain-password",
            },
        )

        assert response.status_code == 204
        assert response.content == b""
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        cookies = _cookie_headers(response)
        assert set(cookies) == {"access_token", "refresh_token"}

        access_cookie = cookies["access_token"]
        assert "access-secret" in access_cookie
        assert "HttpOnly" in access_cookie
        assert "Secure" in access_cookie
        assert "SameSite=lax" in access_cookie
        assert "Max-Age=900" in access_cookie
        assert "Path=/" in access_cookie

        refresh_cookie = cookies["refresh_token"]
        assert "refresh-secret" in refresh_cookie
        assert "HttpOnly" in refresh_cookie
        assert "Secure" in refresh_cookie
        assert "SameSite=lax" in refresh_cookie
        assert "Max-Age=2592000" in refresh_cookie
        assert "Path=/api/v1/auth" in refresh_cookie

        auth_service.login.execute.assert_awaited_once_with(
            LoginCommand(
                email="user@example.com",
                password="plain-password",
            )
        )

    async def test_invalid_credentials_set_no_cookies(self, auth_http) -> None:
        """неуспешный login не меняет cookie клиента."""
        client, auth_service, _app = auth_http
        auth_service.login.execute.side_effect = InvalidCredentialsError()

        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "wrong-password",
            },
        )

        assert response.status_code == 401
        assert "set-cookie" not in response.headers


class TestRefreshCookieEndpoint:
    async def test_guarded_refresh_sets_replayed_pair_as_cookies(
        self, auth_http
    ) -> None:
        client, _auth_service, app = auth_http
        refresh_use_case = app.state.refresh_use_case
        refresh_use_case.execute.return_value = TokenPair(
            access_token="replayed-access-secret",
            refresh_token="replayed-refresh-secret",
        )

        response = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "Cookie": "refresh_token=presented-refresh-secret",
                "Idempotency-Key": "stable-refresh-key",
            },
        )

        assert response.status_code == 204
        assert set(_cookie_headers(response)) == {"access_token", "refresh_token"}
        assert response.headers["Cache-Control"] == "no-store"
        refresh_use_case.execute.assert_awaited_once_with(
            RefreshCommand(
                refresh_token="presented-refresh-secret",
                key_hash=hash_idempotency_key("stable-refresh-key"),
            )
        )

    async def test_stale_refresh_replay_clears_both_cookies(self, auth_http) -> None:
        client, _auth_service, app = auth_http
        app.state.refresh_use_case.execute.side_effect = InvalidRefreshTokenError()

        response = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "Cookie": "refresh_token=stale-refresh-secret",
                "Idempotency-Key": "stale-refresh-key",
            },
        )

        assert response.status_code == 401
        assert response.json()["code"] == "auth.invalid_refresh_token"
        assert set(_cookie_headers(response)) == {"access_token", "refresh_token"}

    @pytest.mark.parametrize(
        ("error", "expected_status", "expected_code", "retry_after"),
        [
            (
                IdempotencyKeyConflictError(),
                409,
                "auth.idempotency_key_conflict",
                None,
            ),
            (
                IdempotencyRequestInProgressError(),
                423,
                "auth.idempotency_request_in_progress",
                "1",
            ),
            (
                IdempotencyStorageUnavailableError(),
                503,
                "auth.idempotency_unavailable",
                None,
            ),
            (
                RefreshReplayUnavailableError(),
                503,
                "auth.refresh_replay_unavailable",
                None,
            ),
        ],
    )
    async def test_idempotency_failures_have_stable_http_contract(
        self,
        auth_http,
        error: Exception,
        expected_status: int,
        expected_code: str,
        retry_after: str | None,
    ) -> None:
        client, _auth_service, app = auth_http
        app.state.refresh_use_case.execute.side_effect = error

        response = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "Cookie": "refresh_token=presented-refresh-secret",
                "Idempotency-Key": "stable-refresh-key",
            },
        )

        assert response.status_code == expected_status
        assert response.json()["code"] == expected_code
        assert response.headers.get("Retry-After") == retry_after
        assert "set-cookie" not in response.headers

    async def test_openapi_describes_cookie_only_security(
        self,
        auth_http,
    ) -> None:
        """production-like OpenAPI отражает cookie-only контракт."""
        _client, _auth_service, app = auth_http

        openapi = app.openapi()
        schemas = openapi["components"]["schemas"]
        security_schemes = openapi["components"]["securitySchemes"]
        paths = openapi["paths"]

        assert "TokenPairResponse" not in schemas
        assert "TestLoginResponse" not in schemas
        assert "RefreshRequest" not in schemas
        assert security_schemes["InternalAccessBearer"] == {
            "type": "http",
            "description": ("Bearer access token injected by the trusted API Gateway"),
            "scheme": "bearer",
        }
        assert security_schemes["RefreshTokenCookie"] == {
            "type": "apiKey",
            "description": ("HttpOnly refresh-token cookie scoped to /api/v1/auth"),
            "in": "cookie",
            "name": "refresh_token",
        }
        assert paths["/api/v1/auth/me"]["get"]["security"] == [
            {"InternalAccessBearer": []}
        ]
        assert paths["/api/v1/auth/refresh"]["post"]["security"] == [
            {"RefreshTokenCookie": []}
        ]
        assert "security" not in paths["/api/v1/auth/login"]["post"]
        assert "/api/v1/auth/login/test" not in paths

        for endpoint in ("login", "refresh", "logout"):
            no_content = paths[f"/api/v1/auth/{endpoint}"]["post"]["responses"]["204"]
            assert "content" not in no_content
            assert "Set-Cookie" in no_content["headers"]

        unauthorized = paths["/api/v1/auth/me"]["get"]["responses"]["401"]
        assert (
            unauthorized["content"]["application/json"]["schema"]["$ref"]
            == "#/components/schemas/ApiErrorResponse"
        )


class TestTokenLoginEndpoint:
    async def test_route_is_absent_when_disabled(self, auth_http) -> None:
        """test token endpoint выключен при fail-closed configuration."""
        client, auth_service, app = auth_http

        response = await client.post(
            "/api/v1/auth/login/test",
            json={
                "email": "user@example.com",
                "password": "plain-password",
            },
        )

        assert response.status_code == 404
        assert "/api/v1/auth/login/test" not in app.openapi()["paths"]
        auth_service.login.execute.assert_not_awaited()

    async def test_returns_tokens_without_cookies(
        self,
        auth_http_with_test_token_endpoint,
    ) -> None:
        """The test endpoint delegates to the same login handler."""
        client, auth_service, app = auth_http_with_test_token_endpoint
        auth_service.login.execute.return_value = TokenPair(
            access_token="access-secret",
            refresh_token="refresh-secret",
        )

        response = await client.post(
            "/api/v1/auth/login/test",
            json={
                "email": "USER@EXAMPLE.COM",
                "password": "plain-password",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "accessToken": "access-secret",
            "refreshToken": "refresh-secret",
        }
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        assert "set-cookie" not in response.headers
        assert "/api/v1/auth/login/test" in app.openapi()["paths"]
        assert "TestLoginResponse" in app.openapi()["components"]["schemas"]
        auth_service.login.execute.assert_awaited_once_with(
            LoginCommand(
                email="user@example.com",
                password="plain-password",
            )
        )

    async def test_invalid_credentials_return_no_tokens(
        self,
        auth_http_with_test_token_endpoint,
    ) -> None:
        """test login сохраняет штатную invalid credentials boundary."""
        client, auth_service, _app = auth_http_with_test_token_endpoint
        auth_service.login.execute.side_effect = InvalidCredentialsError()

        response = await client.post(
            "/api/v1/auth/login/test",
            json={
                "email": "user@example.com",
                "password": "wrong-password",
            },
        )

        assert response.status_code == 401
        assert response.json()["code"] == "auth.invalid_credentials"
        assert "accessToken" not in response.text
        assert "refreshToken" not in response.text
        assert "set-cookie" not in response.headers


class TestTokenLoginConfiguration:
    def test_production_cannot_enable_token_response(self) -> None:
        """production configuration пытается включить test token route."""
        with pytest.raises(InvalidTokenConfigurationError):
            Settings(
                _env_file=None,
                DATABASE_HOST="localhost",
                DATABASE_PORT=5432,
                DATABASE_USER="auth",
                DATABASE_PASSWORD="auth",
                DATABASE_NAME="auth",
                DEV_LOGS=False,
                JWT_PRIVATE_KEY_PATH="private.pem",
                JWT_PUBLIC_KEY_PATH="public.pem",
                JWT_ACTIVE_KEY_ID="test-key",
                APP_ENVIRONMENT="production",
                AUTH_TEST_TOKEN_ENDPOINT_ENABLED=True,
            )

    def test_test_environment_can_enable_token_response(self) -> None:
        """integration test configuration явно включает token route."""
        settings = Settings(
            _env_file=None,
            DATABASE_HOST="localhost",
            DATABASE_PORT=5432,
            DATABASE_USER="auth",
            DATABASE_PASSWORD="auth",
            DATABASE_NAME="auth",
            DEV_LOGS=False,
            JWT_PRIVATE_KEY_PATH="private.pem",
            JWT_PUBLIC_KEY_PATH="public.pem",
            JWT_ACTIVE_KEY_ID="test-key",
            APP_ENVIRONMENT="test",
            AUTH_TEST_TOKEN_ENDPOINT_ENABLED=True,
        )

        assert settings.test_token_endpoint_enabled is True


class TestLogoutCookieEndpoint:
    async def test_revokes_session_and_deletes_both_cookies(
        self,
        auth_http,
    ) -> None:
        """успешный logout закрывает server-side family и browser state."""
        client, auth_service, _app = auth_http

        response = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Cookie": ("access_token=access-secret; refresh_token=refresh-secret")
            },
        )

        assert response.status_code == 204
        assert response.content == b""
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        cookies = _cookie_headers(response)
        assert set(cookies) == {"access_token", "refresh_token"}

        access_cookie = cookies["access_token"]
        assert "access_token=" in access_cookie
        assert "Max-Age=0" in access_cookie
        assert "HttpOnly" in access_cookie
        assert "Secure" in access_cookie
        assert "SameSite=lax" in access_cookie
        assert "Path=/" in access_cookie

        refresh_cookie = cookies["refresh_token"]
        assert "refresh_token=" in refresh_cookie
        assert "Max-Age=0" in refresh_cookie
        assert "HttpOnly" in refresh_cookie
        assert "Secure" in refresh_cookie
        assert "SameSite=lax" in refresh_cookie
        assert "Path=/api/v1/auth" in refresh_cookie
        auth_service.logout.execute.assert_awaited_once_with(
            LogoutCommand(refresh_token="refresh-secret")
        )

    async def test_missing_cookie_is_idempotent_success(self, auth_http) -> None:
        """повторный HTTP logout после удаления refresh-cookie."""
        client, auth_service, _app = auth_http

        response = await client.post("/api/v1/auth/logout")

        assert response.status_code == 204
        assert set(_cookie_headers(response)) == {
            "access_token",
            "refresh_token",
        }
        auth_service.logout.execute.assert_awaited_once_with(LogoutCommand())

    async def test_unknown_refresh_is_safe_idempotent_success(
        self,
        auth_http,
    ) -> None:
        """неизвестная cookie не раскрывает наличие token family."""
        client, auth_service, _app = auth_http
        auth_service.logout.execute.return_value = None

        response = await client.post(
            "/api/v1/auth/logout",
            headers={"Cookie": "refresh_token=unknown-refresh"},
        )

        assert response.status_code == 204
        assert set(_cookie_headers(response)) == {
            "access_token",
            "refresh_token",
        }


class TestMeInternalBearerEndpoint:
    async def test_access_cookie_without_gateway_header_is_rejected(
        self,
        auth_http,
    ) -> None:
        """внутренний /me вызван напрямую с browser cookie."""
        client, auth_service, _app = auth_http

        response = await client.get(
            "/api/v1/auth/me",
            headers={"Cookie": "access_token=access-secret"},
        )

        assert response.status_code == 401
        assert response.json()["code"] == "auth.invalid_token"
        auth_service.get_current_user.execute.assert_not_awaited()

    async def test_returns_current_user_from_gateway_bearer(
        self,
        auth_http,
    ) -> None:
        """Gateway передал access-cookie внутренним Bearer header."""
        client, auth_service, _app = auth_http
        user = User(
            email="user@example.com",
            password_hash="stored-hash",
        )
        auth_service.get_current_user.execute.return_value = user

        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer access-secret"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "id": str(user.id),
            "email": "user@example.com",
            "role": "USER",
            "createdAt": user.created_at.isoformat().replace("+00:00", "Z"),
        }
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
        assert "set-cookie" not in response.headers
        auth_service.get_current_user.execute.assert_awaited_once_with(
            GetCurrentUserQuery(access_token="access-secret")
        )

    async def test_invalid_access_uses_public_unauthorized(
        self,
        auth_http,
    ) -> None:
        """verifier отклоняет access-cookie на HTTP-границе."""
        client, auth_service, _app = auth_http
        auth_service.get_current_user.execute.side_effect = TokenExpiredError()

        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer expired-access"},
        )

        assert response.status_code == 401
        assert response.json()["code"] == "auth.invalid_token"
        assert "set-cookie" not in response.headers
