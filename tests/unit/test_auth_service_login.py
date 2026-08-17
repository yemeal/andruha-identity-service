from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from app.application.ports.dto.security import (
    AccessPrincipal,
    AccessTokenClaims,
    IssuedRefreshToken,
)
from app.application.ports.security import AccessTokenVerifierProtocol
from app.application.services.auth_service import AuthService
from app.core.settings import Settings
from app.domain.auth_sessions import AuthSession
from app.domain.exceptions import (
    DomainErrors,
    InvalidCredentialsError,
    InvalidTokenError,
    UserAlreadyExistsError,
)
from app.domain.refresh_tokens import RefreshToken
from app.domain.users import User


class TrackingUOW:
    def __init__(self) -> None:
        self.active = False
        self.entries = 0
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> TrackingUOW:
        assert not self.active
        self.active = True
        self.entries += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        self.active = False
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1


class LoginUserRepository:
    def __init__(self, uow: TrackingUOW, user: User | None) -> None:
        self._uow = uow
        self.user = user
        self.locked_user: User | None = user
        self.registration_result: User | None = user
        self.lookup_calls = 0
        self.lock_calls = 0

    async def create_if_absent(self, entity: User) -> User | None:
        assert self._uow.active
        if self.registration_result is None:
            return None
        return entity.model_copy(deep=True)

    async def get_by_email(self, email: str) -> User | None:
        assert self._uow.active
        self.lookup_calls += 1
        if self.user is None or self.user.email != email:
            return None
        return self.user.model_copy(deep=True)

    async def get_for_update(self, user_id: UUID) -> User | None:
        assert self._uow.active
        self.lock_calls += 1
        if self.locked_user is None or self.locked_user.id != user_id:
            return None
        return self.locked_user.model_copy(deep=True)

    async def get(self, user_id: UUID) -> User | None:
        assert self._uow.active
        if self.user is None or self.user.id != user_id:
            return None
        return self.user.model_copy(deep=True)


class LoginAuthSessionRepository:
    def __init__(self, uow: TrackingUOW) -> None:
        self._uow = uow
        self.created: list[AuthSession] = []

    async def create(self, entity: AuthSession) -> AuthSession:
        assert self._uow.active
        self.created.append(entity.model_copy(deep=True))
        return entity


class LoginRefreshTokenRepository:
    def __init__(self, uow: TrackingUOW) -> None:
        self._uow = uow
        self.created: list[RefreshToken] = []
        self.fail_create = False

    async def create(self, entity: RefreshToken) -> RefreshToken:
        assert self._uow.active
        if self.fail_create:
            raise RuntimeError("refresh insert failed")
        self.created.append(entity.model_copy(deep=True))
        return entity


class RecordingPasswordHasher:
    def __init__(self, uow: TrackingUOW, *, password_matches: bool = True) -> None:
        self._uow = uow
        self.password_matches = password_matches
        self.calls: list[tuple[str, str | None]] = []
        self.hash_calls: list[str] = []

    async def hash(self, password: str) -> str:
        assert not self._uow.active
        self.hash_calls.append(password)
        return "new-password-hash"

    async def verify(self, _password: str, _password_hash: str) -> bool:
        raise AssertionError("login must use constant-work verification")

    async def verify_or_dummy(
        self,
        password: str,
        password_hash: str | None,
    ) -> bool:
        assert not self._uow.active
        self.calls.append((password, password_hash))
        return password_hash is not None and self.password_matches


class RecordingAccessTokenIssuer:
    def __init__(self) -> None:
        self.principals: list[AccessPrincipal] = []

    def issue(self, principal: AccessPrincipal, now: datetime) -> str:
        self.principals.append(principal)
        return f"access-secret-{principal.user_id}-{int(now.timestamp())}"


class RecordingRefreshTokenCodec:
    def __init__(self) -> None:
        self.issue_calls = 0

    def issue(self) -> IssuedRefreshToken:
        self.issue_calls += 1
        value = f"refresh-secret-{self.issue_calls}"
        return IssuedRefreshToken(value=value, digest=self.digest(value))

    def digest(self, token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()


class UnusedAccessTokenVerifier:
    def verify(self, _token: str) -> AccessTokenClaims:
        raise AssertionError("verifier is not used by login")


class RecordingAccessTokenVerifier:
    def __init__(self) -> None:
        self.user_id: UUID | None = None
        self.tokens: list[str] = []
        self.error: InvalidTokenError | None = None

    def verify(self, token: str) -> AccessTokenClaims:
        self.tokens.append(token)
        if self.error is not None:
            raise self.error
        assert self.user_id is not None
        return AccessTokenClaims.model_construct(user_id=self.user_id)


@dataclass
class LoginScenario:
    service: AuthService
    user: User
    users: LoginUserRepository
    sessions: LoginAuthSessionRepository
    refresh_tokens: LoginRefreshTokenRepository
    hasher: RecordingPasswordHasher
    access_issuer: RecordingAccessTokenIssuer
    refresh_codec: RecordingRefreshTokenCodec
    uow: TrackingUOW
    now: datetime


def create_login_scenario(
    *,
    password_matches: bool = True,
    access_token_verifier: AccessTokenVerifierProtocol | None = None,
) -> LoginScenario:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    user = User(
        email="user@example.com",
        password_hash="stored-password-hash",
    )
    uow = TrackingUOW()
    users = LoginUserRepository(uow, user)
    sessions = LoginAuthSessionRepository(uow)
    refresh_tokens = LoginRefreshTokenRepository(uow)
    hasher = RecordingPasswordHasher(
        uow,
        password_matches=password_matches,
    )
    access_issuer = RecordingAccessTokenIssuer()
    refresh_codec = RecordingRefreshTokenCodec()
    settings = Settings(
        DATABASE_HOST="localhost",
        DATABASE_PORT=5432,
        DATABASE_USER="auth",
        DATABASE_PASSWORD="auth",
        DATABASE_NAME="auth",
        DEV_LOGS=True,
        JWT_PRIVATE_KEY_PATH=Path("unused-private.pem"),
        JWT_PUBLIC_KEY_PATH=Path("unused-public.pem"),
        JWT_ACTIVE_KEY_ID="auth-test",
        JWT_ISSUER="payflow-auth",
        JWT_SERVICE_AUDIENCE="auth-service",
        JWT_AUDIENCES="auth-service",
        AUTH_SESSION_IDLE_TTL_SECONDS=30 * 24 * 60 * 60,
    )
    service = AuthService(
        user_repo=users,
        refresh_token_repo=refresh_tokens,
        auth_session_repo=sessions,
        uow=uow,
        password_hasher=hasher,
        access_token_issuer=access_issuer,
        access_token_verifier=(access_token_verifier or UnusedAccessTokenVerifier()),
        refresh_token_codec=refresh_codec,
        settings=settings,
        clock=lambda: now,
    )
    return LoginScenario(
        service=service,
        user=user,
        users=users,
        sessions=sessions,
        refresh_tokens=refresh_tokens,
        hasher=hasher,
        access_issuer=access_issuer,
        refresh_codec=refresh_codec,
        uow=uow,
        now=now,
    )


class TestLoginAuthentication:
    async def test_success_verifies_password_outside_transaction(self) -> None:
        """
        Проверяем: дорогая проверка пароля не удерживает DB-транзакцию.
        Успех: lookup и запись разделены, session и refresh сохранены атомарно.
        Нежелательное поведение: Argon2 занимает соединение из пула.
        """
        scenario = create_login_scenario()

        pair = await scenario.service.login("user@example.com", "plain-password")

        assert scenario.hasher.calls == [
            ("plain-password", scenario.user.password_hash)
        ]
        assert scenario.uow.entries == 2
        assert scenario.uow.commits == 2
        assert scenario.uow.rollbacks == 0
        assert scenario.users.lock_calls == 1
        assert len(scenario.sessions.created) == 1
        assert len(scenario.refresh_tokens.created) == 1
        assert pair.refresh_token == "refresh-secret-1"

    @pytest.mark.parametrize("user_state", ["missing", "disabled"])
    async def test_unavailable_user_still_performs_dummy_kdf(
        self,
        user_state: str,
    ) -> None:
        """
        Проверяем: отсутствие и блокировка user не создают быстрый путь отказа.
        Успех: hasher получает None и выполняет эквивалентную dummy-KDF.
        Нежелательное поведение: существование email определяется по времени.
        """
        scenario = create_login_scenario()
        if user_state == "missing":
            scenario.users.user = None
        else:
            disabled_user = scenario.user.model_copy(deep=True)
            disabled_user.disable(scenario.now)
            scenario.users.user = disabled_user

        with pytest.raises(InvalidCredentialsError):
            await scenario.service.login("user@example.com", "plain-password")

        assert scenario.hasher.calls == [("plain-password", None)]
        assert scenario.uow.entries == 1
        assert scenario.uow.commits == 1
        assert scenario.access_issuer.principals == []
        assert scenario.refresh_codec.issue_calls == 0

    async def test_wrong_password_uses_real_hash_and_creates_nothing(self) -> None:
        """
        Проверяем: неверный пароль проходит настоящую Argon2-проверку.
        Успех: наружу уходит единая ошибка, токены и сессия не создаются.
        Нежелательное поведение: неверный пароль получает быстрый отдельный путь.
        """
        scenario = create_login_scenario(password_matches=False)

        with pytest.raises(InvalidCredentialsError):
            await scenario.service.login("user@example.com", "wrong-password")

        assert scenario.hasher.calls == [
            ("wrong-password", scenario.user.password_hash)
        ]
        assert scenario.uow.entries == 1
        assert scenario.access_issuer.principals == []
        assert scenario.sessions.created == []
        assert scenario.refresh_tokens.created == []

    async def test_user_is_revalidated_before_tokens_are_issued(self) -> None:
        """
        Проверяем: состояние user могло измениться во время Argon2-проверки.
        Успех: заблокированный под lock user не получает новую пару токенов.
        Нежелательное поведение: токен выпускается из устаревшего снимка user.
        """
        scenario = create_login_scenario()
        disabled_user = scenario.user.model_copy(deep=True)
        disabled_user.disable(scenario.now)
        scenario.users.locked_user = disabled_user

        with pytest.raises(InvalidCredentialsError):
            await scenario.service.login("user@example.com", "plain-password")

        assert scenario.users.lock_calls == 1
        assert scenario.uow.commits == 1
        assert scenario.uow.rollbacks == 1
        assert scenario.access_issuer.principals == []
        assert scenario.refresh_codec.issue_calls == 0


class TestLoginObservability:
    async def test_success_logs_committed_stages_without_secrets(self) -> None:
        """
        Проверяем: успешный login можно восстановить по структурированным логам.
        Успех: terminal-событие идет после commit, секреты в логи не попадают.
        Нежелательное поведение: есть токены в логах или нет login succeeded.
        """
        scenario = create_login_scenario()

        with capture_logs() as logs:
            await scenario.service.login("user@example.com", "plain-password")

        events = {entry["event"] for entry in logs}
        assert {
            "login started",
            "login user locked",
            "access token issued",
            "new refresh token issued",
            "auth session stored",
            "new refresh token stored",
            "login transaction committed",
            "login succeeded",
        } <= events

        rendered_logs = repr(logs)
        assert "user@example.com" not in rendered_logs
        assert "plain-password" not in rendered_logs
        assert scenario.user.password_hash not in rendered_logs
        assert "access-secret" not in rendered_logs
        assert "refresh-secret" not in rendered_logs

    async def test_unexpected_failure_logs_stage_and_traceback(self) -> None:
        """
        Проверяем: инфраструктурный сбой получает operation-level контекст.
        Успех: login failed содержит последнюю стадию и exception.
        Нежелательное поведение: ошибка видна только как безымянный HTTP 500.
        """
        scenario = create_login_scenario()
        scenario.refresh_tokens.fail_create = True

        with (
            capture_logs() as logs,
            pytest.raises(RuntimeError, match="refresh insert failed"),
        ):
            await scenario.service.login("user@example.com", "plain-password")

        failure = next(entry for entry in logs if entry["event"] == "login failed")
        assert failure["stage"] == "refresh token storage"
        assert failure["exc_info"] is True
        assert scenario.uow.rollbacks == 1


class TestRegistrationObservability:
    async def test_expected_conflict_is_not_logged_as_exception(
        self,
    ) -> None:
        """
        Проверяем: duplicate email является ожидаемым domain rejection.
        Успех: есть conflict event без PII и без register failed traceback.
        Нежелательное поведение: email или password попадают в exception log.
        """
        scenario = create_login_scenario()
        scenario.users.registration_result = None
        password = "plain-registration-password"

        with capture_logs() as logs, pytest.raises(UserAlreadyExistsError):
            await scenario.service.register(
                "user@example.com",
                password,
            )

        events = {entry["event"] for entry in logs}
        assert "user registration conflict" in events
        assert "register failed" not in events
        assert scenario.hasher.hash_calls == [password]

        rendered_logs = repr(logs)
        assert "user@example.com" not in rendered_logs
        assert password not in rendered_logs
        assert "new-password-hash" not in rendered_logs


class TestCurrentUser:
    async def test_returns_authenticatable_user_from_verified_claims(self) -> None:
        """
        Проверяем: получение текущего пользователя по access JWT.
        Успех: verifier задает user_id, а чтение user выполняется внутри UOW.
        Нежелательное поведение: сырые claims заменяют актуального пользователя.
        """
        verifier = RecordingAccessTokenVerifier()
        scenario = create_login_scenario(access_token_verifier=verifier)
        verifier.user_id = scenario.user.id

        user = await scenario.service.get_current_user("access-secret")

        assert user == scenario.user
        assert verifier.tokens == ["access-secret"]
        assert scenario.uow.entries == 1
        assert scenario.uow.commits == 1

    @pytest.mark.parametrize("user_state", ["missing", "disabled"])
    async def test_unavailable_user_invalidates_access(
        self,
        user_state: str,
    ) -> None:
        """
        Проверяем: subject JWT удален или больше не может аутентифицироваться.
        Успех: оба состояния возвращают безопасный INVALID_TOKEN.
        Нежелательное поведение: /me раскрывает профиль отключенного пользователя.
        """
        verifier = RecordingAccessTokenVerifier()
        scenario = create_login_scenario(access_token_verifier=verifier)
        verifier.user_id = scenario.user.id
        if user_state == "missing":
            scenario.users.user = None
        else:
            scenario.user.disable(scenario.now)
            scenario.users.user = scenario.user

        with pytest.raises(InvalidTokenError):
            await scenario.service.get_current_user("access-secret")

        assert scenario.uow.commits == 0
        assert scenario.uow.rollbacks == 1

    async def test_invalid_access_does_not_open_uow(self) -> None:
        """
        Проверяем: verifier отклоняет access JWT до обращения к БД.
        Успех: доменная token-ошибка сохраняется, UOW не открывается.
        Нежелательное поведение: невалидный JWT расходует DB connection.
        """
        verifier = RecordingAccessTokenVerifier()
        verifier.error = DomainErrors.Token.EXPIRED()
        scenario = create_login_scenario(access_token_verifier=verifier)

        with pytest.raises(InvalidTokenError):
            await scenario.service.get_current_user("expired-access")

        assert scenario.uow.entries == 0
