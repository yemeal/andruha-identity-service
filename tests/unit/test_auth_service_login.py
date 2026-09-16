from __future__ import annotations
from app.domain.exceptions import InvalidCredentialsError
from app.application.exceptions.security import TokenExpiredError, InvalidTokenError

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from app.application.use_cases.login.handler import LoginHandler
from app.application.use_cases.login.command import LoginCommand
from app.application.use_cases.get_current_user.handler import GetCurrentUserHandler
from app.application.use_cases.get_current_user.query import GetCurrentUserQuery
from app.application.tokens import TokenPairIssuer
from app.domain.aggregates.auth_session import AuthSession
from app.domain.entities.refresh_token import RefreshToken
from app.domain.aggregates.user import User


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
    def __init__(self, uow: TrackingUOW, tokens: LoginRefreshTokenRepository) -> None:
        self._uow = uow
        self._tokens = tokens
        self.created: list[AuthSession] = []

    async def create(self, entity: AuthSession) -> AuthSession:
        assert self._uow.active
        self.created.append(entity.model_copy(deep=True))
        assert entity.refresh_token is not None
        await self._tokens.create(entity.refresh_token)
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
    service: LoginHandler
    current_user: GetCurrentUserHandler
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
        created_at=now,
    )
    uow = TrackingUOW()
    users = LoginUserRepository(uow, user)
    refresh_tokens = LoginRefreshTokenRepository(uow)
    sessions = LoginAuthSessionRepository(uow, refresh_tokens)
    hasher = RecordingPasswordHasher(
        uow,
        password_matches=password_matches,
    )
    access_issuer = RecordingAccessTokenIssuer()
    refresh_codec = RecordingRefreshTokenCodec()
    service = LoginHandler(
        users=users,
        sessions=sessions,
        uow=uow,
        password_hasher=hasher,
        issuer=TokenPairIssuer(access_issuer, refresh_codec),
        session_idle_ttl=timedelta(days=30),
        clock=lambda: now,
    )
    current_user = GetCurrentUserHandler(
        users, access_token_verifier or UnusedAccessTokenVerifier(), uow
    )
    return LoginScenario(
        service=service,
        current_user=current_user,
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
        """дорогая проверка пароля не удерживает DB-транзакцию."""
        scenario = create_login_scenario()

        pair = await scenario.service.execute(
            LoginCommand(email="user@example.com", password="plain-password")
        )

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
        """отсутствие и блокировка user не создают быстрый путь отказа."""
        scenario = create_login_scenario()
        if user_state == "missing":
            scenario.users.user = None
        else:
            disabled_user = scenario.user.model_copy(deep=True)
            disabled_user.disable(scenario.now)
            scenario.users.user = disabled_user

        with pytest.raises(InvalidCredentialsError):
            await scenario.service.execute(
                LoginCommand(email="user@example.com", password="plain-password")
            )

        assert scenario.hasher.calls == [("plain-password", None)]
        assert scenario.uow.entries == 1
        assert scenario.uow.commits == 1
        assert scenario.access_issuer.principals == []
        assert scenario.refresh_codec.issue_calls == 0

    async def test_wrong_password_uses_real_hash_and_creates_nothing(self) -> None:
        """неверный пароль проходит настоящую Argon2-проверку."""
        scenario = create_login_scenario(password_matches=False)

        with pytest.raises(InvalidCredentialsError):
            await scenario.service.execute(
                LoginCommand(email="user@example.com", password="wrong-password")
            )

        assert scenario.hasher.calls == [
            ("wrong-password", scenario.user.password_hash)
        ]
        assert scenario.uow.entries == 1
        assert scenario.access_issuer.principals == []
        assert scenario.sessions.created == []
        assert scenario.refresh_tokens.created == []

    async def test_user_is_revalidated_before_tokens_are_issued(self) -> None:
        """состояние user могло измениться во время Argon2-проверки."""
        scenario = create_login_scenario()
        disabled_user = scenario.user.model_copy(deep=True)
        disabled_user.disable(scenario.now)
        scenario.users.locked_user = disabled_user

        with pytest.raises(InvalidCredentialsError):
            await scenario.service.execute(
                LoginCommand(email="user@example.com", password="plain-password")
            )

        assert scenario.users.lock_calls == 1
        assert scenario.uow.commits == 1
        assert scenario.uow.rollbacks == 1
        assert scenario.access_issuer.principals == []
        assert scenario.refresh_codec.issue_calls == 0


class TestLoginObservability:
    async def test_success_logs_committed_stages_without_secrets(self) -> None:
        """успешный login можно восстановить по структурированным логам."""
        scenario = create_login_scenario()

        with capture_logs() as logs:
            await scenario.service.execute(
                LoginCommand(email="user@example.com", password="plain-password")
            )

        events = {entry["event"] for entry in logs}
        assert events == {"login succeeded"}

        rendered_logs = repr(logs)
        assert "user@example.com" not in rendered_logs
        assert "plain-password" not in rendered_logs
        assert scenario.user.password_hash not in rendered_logs
        assert "access-secret" not in rendered_logs
        assert "refresh-secret" not in rendered_logs

    async def test_failure_rolls_back_without_success_event(self) -> None:
        """A failed write rolls back and never emits login success."""
        scenario = create_login_scenario()
        scenario.refresh_tokens.fail_create = True

        with (
            capture_logs() as logs,
            pytest.raises(RuntimeError, match="refresh insert failed"),
        ):
            await scenario.service.execute(
                LoginCommand(email="user@example.com", password="plain-password")
            )

        assert not any(entry["event"] == "login succeeded" for entry in logs)
        assert scenario.uow.rollbacks == 1


class TestCurrentUser:
    async def test_returns_authenticatable_user_from_verified_claims(self) -> None:
        """получение текущего пользователя по access JWT."""
        verifier = RecordingAccessTokenVerifier()
        scenario = create_login_scenario(access_token_verifier=verifier)
        verifier.user_id = scenario.user.id

        user = await scenario.current_user.execute(
            GetCurrentUserQuery(access_token="access-secret")
        )

        assert user.id == scenario.user.id
        assert user.email == scenario.user.email
        assert not hasattr(user, "password_hash")
        assert verifier.tokens == ["access-secret"]
        assert scenario.uow.entries == 1
        assert scenario.uow.commits == 1

    @pytest.mark.parametrize("user_state", ["missing", "disabled"])
    async def test_unavailable_user_invalidates_access(
        self,
        user_state: str,
    ) -> None:
        """subject JWT удален или больше не может аутентифицироваться."""
        verifier = RecordingAccessTokenVerifier()
        scenario = create_login_scenario(access_token_verifier=verifier)
        verifier.user_id = scenario.user.id
        if user_state == "missing":
            scenario.users.user = None
        else:
            scenario.user.disable(scenario.now)
            scenario.users.user = scenario.user

        with pytest.raises(InvalidTokenError):
            await scenario.current_user.execute(
                GetCurrentUserQuery(access_token="access-secret")
            )

        assert scenario.uow.commits == 0
        assert scenario.uow.rollbacks == 1

    async def test_invalid_access_does_not_open_uow(self) -> None:
        """verifier отклоняет access JWT до обращения к БД."""
        verifier = RecordingAccessTokenVerifier()
        verifier.error = TokenExpiredError()
        scenario = create_login_scenario(access_token_verifier=verifier)

        with pytest.raises(InvalidTokenError):
            await scenario.current_user.execute(
                GetCurrentUserQuery(access_token="expired-access")
            )

        assert scenario.uow.entries == 0
