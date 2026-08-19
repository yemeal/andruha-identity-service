from __future__ import annotations

import asyncio
import hashlib
from contextvars import ContextVar
from contextvars import Token as ContextToken
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID

import jwt.api_jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from structlog.testing import capture_logs

from app.application.ports.dto.security import (
    AccessPrincipal,
    AccessTokenClaims,
    IssuedRefreshToken,
)
from app.application.ports.events import UserRegisteredEvent
from app.application.services.auth_service import AuthService, TokenPair
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.services.refresh import (
    PUBLIC_REFRESH_SUBJECT,
    REFRESH_OPERATION,
    REJECTED_RESULT,
    SUCCESS_RESULT,
    TransactionalRefreshOperation,
    replay_aad,
)
from app.application.value_objects.idempotency import IdempotencyIdentity
from app.domain.auth_sessions import AuthSession
from app.domain.exceptions import (
    AuthSessionInactiveError,
    InvalidRefreshTokenError,
    TokenExpiredError,
)
from app.domain.refresh_tokens import RefreshToken
from app.domain.users import User, UserRole
from app.infrastructure.security.access_token_issuer import (
    PyJWTAccessTokenIssuer,
)
from app.infrastructure.security.access_token_verifier import (
    PyJWTAccessTokenVerifier,
)


class RefreshInsertError(RuntimeError):
    """Ожидаемый сбой вставки нового refresh-токена в red-тесте."""


@dataclass
class _Transaction:
    token_writes: dict[UUID, RefreshToken] = field(default_factory=dict)
    session_writes: dict[UUID, AuthSession] = field(default_factory=dict)
    locks: list[asyncio.Lock] = field(default_factory=list)
    context_token: ContextToken[_Transaction | None] | None = None


class InMemoryAuthState:
    """
    Минимальное транзакционное состояние для application-тестов.

    Записи видны другим запросам только после commit. Row-lock удерживается до
    выхода из UOW, как SELECT FOR UPDATE в PostgreSQL.
    """

    def __init__(self) -> None:
        self.tokens: dict[UUID, RefreshToken] = {}
        self.sessions: dict[UUID, AuthSession] = {}
        self.token_locks: dict[bytes, asyncio.Lock] = {}
        self.session_locks: dict[UUID, asyncio.Lock] = {}
        self.current_transaction: ContextVar[_Transaction | None] = ContextVar(
            "auth_test_transaction",
            default=None,
        )
        self.commits = 0
        self.rollbacks = 0

    def transaction(self) -> _Transaction:
        transaction = self.current_transaction.get()
        if transaction is None:
            raise AssertionError("Repository call outside UOW")
        return transaction

    async def acquire(self, lock: asyncio.Lock) -> None:
        transaction = self.transaction()
        if lock in transaction.locks:
            return
        await lock.acquire()
        transaction.locks.append(lock)

    def visible_tokens(self) -> dict[UUID, RefreshToken]:
        transaction = self.transaction()
        return {
            **self.tokens,
            **transaction.token_writes,
        }

    def visible_sessions(self) -> dict[UUID, AuthSession]:
        transaction = self.transaction()
        return {
            **self.sessions,
            **transaction.session_writes,
        }


class InMemoryAuthUOW:
    def __init__(self, state: InMemoryAuthState) -> None:
        self._state = state

    async def __aenter__(self) -> InMemoryAuthUOW:
        transaction = _Transaction()
        transaction.context_token = self._state.current_transaction.set(transaction)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        transaction = self._state.transaction()
        try:
            if exc_type is None:
                self._state.tokens.update(
                    {
                        entity_id: entity.model_copy(deep=True)
                        for entity_id, entity in transaction.token_writes.items()
                    }
                )
                self._state.sessions.update(
                    {
                        entity_id: entity.model_copy(deep=True)
                        for entity_id, entity in transaction.session_writes.items()
                    }
                )
                self._state.commits += 1
            else:
                self._state.rollbacks += 1
        finally:
            for lock in reversed(transaction.locks):
                lock.release()
            assert transaction.context_token is not None
            self._state.current_transaction.reset(transaction.context_token)


class InMemoryRefreshTokenRepository:
    def __init__(self, state: InMemoryAuthState) -> None:
        self._state = state
        self.create_calls = 0
        self.fail_next_create = False

    async def get_by_hash_for_update(
        self,
        token_hash: bytes,
    ) -> RefreshToken | None:
        lock = self._state.token_locks.setdefault(
            token_hash,
            asyncio.Lock(),
        )
        await self._state.acquire(lock)

        for token in self._state.visible_tokens().values():
            if token.token_hash == token_hash:
                return token.model_copy(deep=True)
        return None

    async def create(self, entity: RefreshToken) -> RefreshToken:
        self.create_calls += 1
        if self.fail_next_create:
            self.fail_next_create = False
            raise RefreshInsertError
        self._state.transaction().token_writes[entity.id] = entity.model_copy(deep=True)
        return entity

    async def update(self, entity: RefreshToken) -> RefreshToken:
        self._state.transaction().token_writes[entity.id] = entity.model_copy(deep=True)
        return entity

    async def get(self, entity_id: UUID) -> RefreshToken | None:
        entity = self._state.visible_tokens().get(entity_id)
        return entity.model_copy(deep=True) if entity is not None else None


class InMemoryAuthSessionRepository:
    def __init__(self, state: InMemoryAuthState) -> None:
        self._state = state

    async def get_for_update(
        self,
        session_id: UUID,
    ) -> AuthSession | None:
        lock = self._state.session_locks.setdefault(
            session_id,
            asyncio.Lock(),
        )
        await self._state.acquire(lock)

        entity = self._state.visible_sessions().get(session_id)
        return entity.model_copy(deep=True) if entity is not None else None

    async def update(self, entity: AuthSession) -> AuthSession:
        self._state.transaction().session_writes[entity.id] = entity.model_copy(
            deep=True
        )
        return entity

    async def get(self, entity_id: UUID) -> AuthSession | None:
        entity = self._state.visible_sessions().get(entity_id)
        return entity.model_copy(deep=True) if entity is not None else None

    async def create(self, entity: AuthSession) -> AuthSession:
        self._state.transaction().session_writes[entity.id] = entity.model_copy(
            deep=True
        )
        return entity


class InMemoryUserRepository:
    def __init__(self, user: User | None) -> None:
        self.user = user

    async def get(self, user_id: UUID) -> User | None:
        if self.user is None or user_id != self.user.id:
            return None
        return self.user.model_copy(deep=True)

    async def get_by_email(self, email) -> User | None:
        if self.user is None or email != self.user.email:
            return None
        return self.user.model_copy(deep=True)


class FakeRefreshTokenCodec:
    def __init__(self) -> None:
        self.issue_calls = 0

    def issue(self) -> IssuedRefreshToken:
        self.issue_calls += 1
        value = f"new-refresh-{self.issue_calls}"
        return IssuedRefreshToken(
            value=value,
            digest=self.digest(value),
        )

    def digest(self, token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()


class FakeAccessTokenIssuer:
    def __init__(self) -> None:
        self.issue_calls = 0

    def issue(
        self,
        principal: AccessPrincipal,
        now: datetime,
    ) -> str:
        self.issue_calls += 1
        return f"access-{principal.user_id}-{int(now.timestamp())}"


class UnusedAccessTokenVerifier:
    def verify(self, _token: str) -> AccessTokenClaims:
        raise AssertionError("Verifier is not used by refresh/logout")


class UnusedPasswordHasher:
    async def hash(self, _password: str) -> str:
        raise AssertionError("Password hasher is not used by refresh/logout")


class PassthroughReplayProtector:
    def protect(self, payload: dict[str, str], *, aad: bytes) -> dict[str, object]:
        return {"payload": payload, "aad": aad.hex()}

    def restore(self, envelope: dict[str, object], *, aad: bytes) -> dict[str, str]:
        if envelope.get("aad") != aad.hex():
            raise ValueError("AAD mismatch")
        payload = envelope.get("payload")
        assert isinstance(payload, dict)
        return {str(key): str(value) for key, value in payload.items()}

    async def verify(self, _password: str, _password_hash: str) -> bool:
        raise AssertionError("Password hasher is not used by refresh/logout")

    async def verify_or_dummy(
        self,
        _password: str,
        _password_hash: str | None,
    ) -> bool:
        raise AssertionError("Password hasher is not used by refresh/logout")


@dataclass(frozen=True)
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@dataclass
class SessionScenario:
    service: AuthService
    state: InMemoryAuthState
    refresh_tokens: InMemoryRefreshTokenRepository
    sessions: InMemoryAuthSessionRepository
    codec: FakeRefreshTokenCodec
    access_issuer: FakeAccessTokenIssuer
    users: InMemoryUserRepository
    clock: FrozenClock
    user: User
    session: AuthSession
    old_token: RefreshToken
    old_token_value: str
    idle_ttl: timedelta
    operation: TransactionalRefreshOperation
    uow: InMemoryAuthUOW
    protector: PassthroughReplayProtector

    async def refresh(self, raw_token: str) -> TokenPair:
        identity = IdempotencyIdentity(
            subject_id=PUBLIC_REFRESH_SUBJECT,
            operation=REFRESH_OPERATION,
            key_hash=hash_idempotency_key("test-refresh-key"),
        )
        request_hash = compute_request_hash(
            {"refresh_token_digest": self.codec.digest(raw_token).hex()}
        )
        async with self.uow:
            result = await self.operation.execute(raw_token, identity, request_hash)
        if result.result_type == REJECTED_RESULT:
            raise InvalidRefreshTokenError()
        assert result.result_type == SUCCESS_RESULT
        assert result.result_payload is not None
        payload = self.protector.restore(
            result.result_payload,
            aad=replay_aad(identity, request_hash, SUCCESS_RESULT),
        )
        return TokenPair.model_validate(payload)


class FakeEventPublisher:
    def __init__(self) -> None:
        self.published: list[UserRegisteredEvent] = []

    async def publish(self, event: UserRegisteredEvent) -> None:
        self.published.append(event)


def create_scenario(
    *,
    expired: bool = False,
    revoked: bool = False,
) -> SessionScenario:
    now = datetime.now(UTC).replace(microsecond=0)
    idle_ttl = timedelta(days=30)
    user = User(
        email="user@example.com",
        password_hash="unused-password-hash",
        role=UserRole.USER,
    )
    session = AuthSession(
        user_id=user.id,
        idle_expires_at=(
            now - timedelta(seconds=1) if expired else now + timedelta(days=1)
        ),
        revoked_at=now - timedelta(minutes=1) if revoked else None,
    )
    codec = FakeRefreshTokenCodec()
    old_token_value = "old-refresh-token"
    old_token = RefreshToken(
        session_id=session.id,
        token_hash=codec.digest(old_token_value),
    )

    state = InMemoryAuthState()
    state.sessions[session.id] = session.model_copy(deep=True)
    state.tokens[old_token.id] = old_token.model_copy(deep=True)

    refresh_tokens = InMemoryRefreshTokenRepository(state)
    sessions = InMemoryAuthSessionRepository(state)
    users = InMemoryUserRepository(user)
    access_issuer = FakeAccessTokenIssuer()
    clock = FrozenClock(now)
    uow = InMemoryAuthUOW(state)
    protector = PassthroughReplayProtector()
    event_publisher = FakeEventPublisher()
    operation = TransactionalRefreshOperation(
        users,
        refresh_tokens,
        sessions,
        access_issuer,
        codec,
        protector,
        session_idle_ttl=idle_ttl,
        clock=clock.now,
    )
    service = AuthService(
        user_repo=users,
        refresh_token_repo=refresh_tokens,
        auth_session_repo=sessions,
        uow=uow,
        password_hasher=UnusedPasswordHasher(),
        access_token_issuer=access_issuer,
        access_token_verifier=UnusedAccessTokenVerifier(),
        refresh_token_codec=codec,
        event_publisher=event_publisher,
        session_idle_ttl=idle_ttl,
        clock=clock.now,
    )

    return SessionScenario(
        service=service,
        state=state,
        refresh_tokens=refresh_tokens,
        sessions=sessions,
        codec=codec,
        access_issuer=access_issuer,
        users=users,
        clock=clock,
        user=user,
        session=session,
        old_token=old_token,
        old_token_value=old_token_value,
        idle_ttl=idle_ttl,
        operation=operation,
        uow=uow,
        protector=protector,
    )


class TestRefreshRotationContract:
    @pytest.mark.parametrize("user_state", ["missing", "disabled"])
    async def test_unavailable_user_revokes_session_before_safe_error(
        self,
        user_state: str,
    ) -> None:
        """
        Проверяем: удалённый или заблокированный user не продолжает сессию.
        Снаружи состояние user не раскрываем - возвращаем INVALID_REFRESH.
        """
        scenario = create_scenario()
        if user_state == "missing":
            scenario.users.user = None
        else:
            disabled_user = scenario.user.model_copy(deep=True)
            disabled_user.disable(scenario.clock.now())
            scenario.users.user = disabled_user

        with pytest.raises(InvalidRefreshTokenError):
            await scenario.refresh(scenario.old_token_value)

        stored_session = scenario.state.sessions[scenario.session.id]
        assert stored_session.revoked_at == scenario.clock.now()
        assert scenario.state.commits == 1
        assert scenario.state.rollbacks == 0
        assert scenario.codec.issue_calls == 0
        assert scenario.access_issuer.issue_calls == 0

    async def test_success_logs_rotation_stages_without_secrets(self) -> None:
        """
        Проверяем: по логам можно восстановить этапы ротации.
        Сырой refresh и его digest при этом никогда не логируются.
        """
        scenario = create_scenario()

        with capture_logs() as logs:
            await scenario.refresh(scenario.old_token_value)

        rendered_logs = repr(logs)
        assert scenario.old_token_value not in rendered_logs
        assert scenario.old_token.token_hash.hex() not in rendered_logs

    async def test_arbitrary_unicode_token_is_invalid_refresh(self) -> None:
        """
        Проверяем: клиент может прислать не только ASCII.
        Успех: получаем доменную ошибку, а не UnicodeEncodeError/500.
        """
        scenario = create_scenario()

        with pytest.raises(InvalidRefreshTokenError):
            await scenario.refresh("совсем-не-refresh")

        assert scenario.codec.issue_calls == 0
        assert scenario.refresh_tokens.create_calls == 0

    async def test_two_concurrent_refreshes_issue_only_one_token(self) -> None:
        """
        Проверяем: два одновременных запроса с одним refresh-токеном.
        Успех: один запрос получает пару, второй сообщает reuse.
        Нежелательное поведение: оба запроса выпускают новые refresh-токены.
        """
        scenario = create_scenario()

        results = await asyncio.gather(
            scenario.refresh(scenario.old_token_value),
            scenario.refresh(scenario.old_token_value),
            return_exceptions=True,
        )

        successful = [result for result in results if isinstance(result, TokenPair)]
        reused = [
            result for result in results if isinstance(result, InvalidRefreshTokenError)
        ]
        assert len(successful) == 1
        assert len(reused) == 1
        assert scenario.codec.issue_calls == 1
        assert scenario.refresh_tokens.create_calls == 1

    async def test_reuse_commits_family_revoke_before_error(self) -> None:
        """
        Проверяем: повторное предъявление уже использованного refresh-токена.
        Успех: одна family отозвана и commit завершен до доменной ошибки.
        Нежелательное поведение: последующий 401 откатывает отзыв сессии.
        """
        scenario = create_scenario()
        await scenario.refresh(scenario.old_token_value)
        commits_after_rotation = scenario.state.commits
        another_session = AuthSession(
            user_id=scenario.user.id,
            idle_expires_at=scenario.clock.now() + timedelta(days=1),
        )
        scenario.state.sessions[another_session.id] = another_session

        with pytest.raises(InvalidRefreshTokenError):
            await scenario.refresh(scenario.old_token_value)

        stored_session = scenario.state.sessions[scenario.session.id]
        assert stored_session.revoked_at == scenario.clock.now()
        assert scenario.state.sessions[another_session.id].revoked_at is None
        assert scenario.state.commits == commits_after_rotation + 1
        assert scenario.state.rollbacks == 0

    @pytest.mark.parametrize(
        ("expired", "revoked"),
        [(True, False), (False, True)],
    )
    async def test_inactive_session_cannot_rotate(
        self,
        expired: bool,
        revoked: bool,
    ) -> None:
        """
        Проверяем: refresh для истекшей и явно отозванной AuthSession.
        Успех: оба состояния сообщают AuthSessionInactiveError.
        Нежелательное поведение: неактивная сессия выпускает новую пару.
        """
        scenario = create_scenario(expired=expired, revoked=revoked)

        with pytest.raises(AuthSessionInactiveError):
            await scenario.refresh(scenario.old_token_value)

        assert scenario.codec.issue_calls == 0
        assert scenario.refresh_tokens.create_calls == 0

    async def test_insert_failure_rolls_back_whole_rotation(self) -> None:
        """
        Проверяем: атомарность при ошибке вставки нового refresh-токена.
        Успех: старый токен и idle-срок остаются без изменений.
        Нежелательное поведение: старый токен потерян без выпущенной замены.
        """
        scenario = create_scenario()
        old_idle_deadline = scenario.session.idle_expires_at
        scenario.refresh_tokens.fail_next_create = True

        with pytest.raises(RefreshInsertError):
            await scenario.refresh(scenario.old_token_value)

        stored_old_token = scenario.state.tokens[scenario.old_token.id]
        stored_session = scenario.state.sessions[scenario.session.id]
        assert stored_old_token.used_at is None
        assert stored_session.idle_expires_at == old_idle_deadline
        assert len(scenario.state.tokens) == 1
        assert scenario.state.commits == 0
        assert scenario.state.rollbacks == 1

    async def test_rotation_keeps_session_and_extends_only_idle_deadline(
        self,
    ) -> None:
        """
        Проверяем: idle-only контракт успешной ротации.
        Успех: новый токен остается в той же сессии и продлевает idle-срок.
        Нежелательное поведение: появляется новая family или абсолютный expires_at.
        """
        scenario = create_scenario()

        pair = await scenario.refresh(scenario.old_token_value)

        new_tokens = [
            token
            for token in scenario.state.tokens.values()
            if token.id != scenario.old_token.id
        ]
        assert pair.refresh_token == "new-refresh-1"
        assert len(new_tokens) == 1
        assert new_tokens[0].session_id == scenario.session.id
        assert (
            scenario.state.sessions[scenario.session.id].idle_expires_at
            == scenario.clock.now() + scenario.idle_ttl
        )
        assert "absolute_expires_at" not in AuthSession.model_fields
        assert "expires_at" not in RefreshToken.model_fields

    async def test_used_refresh_remains_while_session_exists(self) -> None:
        """
        Проверяем: хранение старого токена после успешной ротации.
        Успех: использованный токен остается рядом с новой записью family.
        Нежелательное поведение: удаление старого токена ломает reuse detection.
        """
        scenario = create_scenario()

        await scenario.refresh(scenario.old_token_value)

        stored_old_token = scenario.state.tokens[scenario.old_token.id]
        assert scenario.session.id in scenario.state.sessions
        assert stored_old_token.used_at == scenario.clock.now()
        assert len(scenario.state.tokens) == 2


class TestLogoutContract:
    async def test_success_log_distinguishes_action_from_timestamp(
        self,
    ) -> None:
        """
        Проверяем: terminal logout log описывает результат отзыва.
        Успех: revoked_now является bool, revoked_at является datetime.
        Нежелательное поведение: bool записывается под именем timestamp.
        """
        scenario = create_scenario()

        with capture_logs() as logs:
            await scenario.service.logout(scenario.old_token_value)

        success = next(entry for entry in logs if entry["event"] == "logout succeeded")
        assert success["revoked_now"] is True
        assert success["revoked_at"] == scenario.clock.now()

    async def test_logout_is_idempotent(self) -> None:
        """
        Проверяем: повторный logout с тем же refresh-токеном.
        Успех: оба вызова завершаются успешно, время отзыва не меняется.
        Нежелательное поведение: второй вызов возвращает ошибку или оживляет сессию.
        """
        scenario = create_scenario()

        await scenario.service.logout(scenario.old_token_value)
        first_revoked_at = scenario.state.sessions[scenario.session.id].revoked_at
        await scenario.service.logout(scenario.old_token_value)

        assert first_revoked_at == scenario.clock.now()
        assert (
            scenario.state.sessions[scenario.session.id].revoked_at == first_revoked_at
        )
        assert scenario.state.commits == 2

    async def test_used_refresh_token_can_logout_its_family(self) -> None:
        """
        Проверяем: logout по уже использованному refresh-токену.
        Успех: связанная с ним family отзывается без reuse-ошибки.
        Нежелательное поведение: logout применяет refresh-policy ротации.
        """
        scenario = create_scenario()
        stored_token = scenario.state.tokens[scenario.old_token.id]
        stored_token.consume(scenario.clock.now() - timedelta(seconds=1))

        await scenario.service.logout(scenario.old_token_value)

        stored_session = scenario.state.sessions[scenario.session.id]
        assert stored_session.revoked_at == scenario.clock.now()
        assert scenario.state.commits == 1
        assert scenario.state.rollbacks == 0

    async def test_unavailable_user_does_not_block_logout(self) -> None:
        """
        Проверяем: logout сессии удаленного или отключенного пользователя.
        Успех: известная token family отзывается независимо от user-state.
        Нежелательное поведение: logout требует refresh-валидацию пользователя.
        """
        scenario = create_scenario()
        scenario.users.user = None

        await scenario.service.logout(scenario.old_token_value)

        stored_session = scenario.state.sessions[scenario.session.id]
        assert stored_session.revoked_at == scenario.clock.now()
        assert scenario.state.commits == 1

    async def test_unknown_refresh_token_is_rejected_inside_uow(self) -> None:
        """
        Проверяем: logout с неизвестным refresh-токеном.
        Успех: наружу выходит единый INVALID_REFRESH после rollback.
        Нежелательное поведение: repository lock выполняется вне UOW.
        """
        scenario = create_scenario()

        with pytest.raises(InvalidRefreshTokenError):
            await scenario.service.logout("unknown-refresh-token")

        assert scenario.state.commits == 0
        assert scenario.state.rollbacks == 1

    async def test_missing_session_is_safe_invalid_refresh(self) -> None:
        """
        Проверяем: refresh-запись с отсутствующей AuthSession.
        Успех: logout возвращает безопасный INVALID_REFRESH.
        Нежелательное поведение: наружу утекает состояние хранилища.
        """
        scenario = create_scenario()
        del scenario.state.sessions[scenario.session.id]

        with pytest.raises(InvalidRefreshTokenError):
            await scenario.service.logout(scenario.old_token_value)

        assert scenario.state.commits == 0
        assert scenario.state.rollbacks == 1

    async def test_concurrent_refresh_and_logout_serialize_family(self) -> None:
        """
        Проверяем: конкурентные refresh и logout одной token family.
        Успех: общий порядок lock исключает ротацию после успешного отзыва.
        Нежелательное поведение: операции взаимно обходят блокировку сессии.
        """
        scenario = create_scenario()

        refresh_result, logout_result = await asyncio.gather(
            scenario.refresh(scenario.old_token_value),
            scenario.service.logout(scenario.old_token_value),
            return_exceptions=True,
        )

        assert logout_result is None
        assert isinstance(
            refresh_result,
            TokenPair | AuthSessionInactiveError,
        )
        assert scenario.state.sessions[scenario.session.id].revoked_at is not None
        assert scenario.codec.issue_calls <= 1

    async def test_access_survives_logout_only_until_its_exp(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Проверяем: stateless access-токен после logout refresh-сессии.
        Успех: logout его не отзывает, но после исходного exp verifier отклоняет.
        Нежелательное поведение: logout требует denylist или продлевает access TTL.
        """
        scenario = create_scenario()
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        access_ttl = timedelta(minutes=15)
        issuer = PyJWTAccessTokenIssuer(
            private_key=private_key,
            key_id="auth-test",
            issuer="payflow-auth",
            audiences=frozenset({"auth-service"}),
            access_token_ttl=access_ttl,
        )
        verifier = PyJWTAccessTokenVerifier(
            public_keys={"auth-test": private_key.public_key()},
            issuer="payflow-auth",
            audience="auth-service",
        )
        access_token = issuer.issue(
            AccessPrincipal(
                user_id=scenario.user.id,
                role=scenario.user.role,
            ),
            scenario.clock.now(),
        )

        await scenario.service.logout(scenario.old_token_value)

        claims = verifier.verify(access_token)
        assert claims.expires_at == scenario.clock.now() + access_ttl

        future = claims.expires_at + timedelta(seconds=1)

        class FutureDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return future.replace(tzinfo=None)
                return future.astimezone(tz)

        monkeypatch.setattr(jwt.api_jwt, "datetime", FutureDatetime)

        with pytest.raises(TokenExpiredError):
            verifier.verify(access_token)
