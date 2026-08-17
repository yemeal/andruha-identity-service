from collections.abc import Callable
from datetime import datetime, timedelta
from time import perf_counter
from typing import Protocol
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field
from structlog.typing import FilteringBoundLogger

from app.application.ports.dto import AccessPrincipal
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    RefreshTokenRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    AccessTokenVerifierProtocol,
    OpaqueRefreshTokenCodecProtocol,
    PasswordHasherProtocol,
)
from app.application.ports.uow import AsyncUOWProtocol
from app.core.settings import Settings
from app.domain.auth_sessions import AuthSession
from app.domain.base import utc_now
from app.domain.exceptions import (
    DomainError,
    DomainErrors,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTokenError,
)
from app.domain.refresh_tokens import RefreshToken
from app.domain.users import User
from app.domain.value_objects.email import NormalizedEmail

logger = structlog.get_logger()


class TokenPair(BaseModel):
    """Результат login/refresh: пара токенов"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class _IssuedTokens(BaseModel):
    """
    Пара токенов, которые мы отдаем клиенту и наш внутренний хеш рефреш-токена
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_pair: TokenPair = Field(repr=False)
    refresh_token_digest: bytes = Field(
        min_length=32,
        max_length=32,
        repr=False,
    )


class _LockedTokenFamily(BaseModel):
    """
    Актуальный рефреш токен и сессия (семейство), к которой он принадлежит
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    refresh_token: RefreshToken = Field(repr=False)
    auth_session: AuthSession = Field(repr=False)


class AuthServiceProtocol(Protocol):
    async def register(self, email: NormalizedEmail, password: str) -> User: ...

    async def login(self, email: NormalizedEmail, password: str) -> TokenPair: ...

    async def logout(self, refresh_token: str) -> None: ...

    async def get_current_user(self, access_token: str) -> User: ...


class AuthService:
    def __init__(
        self,
        user_repo: UserRepositoryProtocol,
        refresh_token_repo: RefreshTokenRepositoryProtocol,
        auth_session_repo: AuthSessionRepositoryProtocol,
        uow: AsyncUOWProtocol,
        password_hasher: PasswordHasherProtocol,
        access_token_issuer: AccessTokenIssuerProtocol,
        access_token_verifier: AccessTokenVerifierProtocol,
        refresh_token_codec: OpaqueRefreshTokenCodecProtocol,
        settings: Settings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._user_repo = user_repo
        self._refresh_token_repo = refresh_token_repo
        self._auth_session_repo = auth_session_repo
        self._uow = uow
        self._password_hasher = password_hasher
        self._access_token_issuer = access_token_issuer
        self._access_token_verifier = access_token_verifier
        self._refresh_token_codec = refresh_token_codec
        self._settings = settings
        self._clock = clock

    async def _verify_candidate_password(
        self,
        candidate_user: User | None,
        password: str,
    ) -> str | None:
        """
        Verify the candidate password against the stored hash.
        """
        password_hash = (
            candidate_user.password_hash
            if candidate_user is not None and candidate_user.can_authenticate
            else None
        )
        is_password_correct = await self._password_hasher.verify_or_dummy(
            password,
            password_hash,
        )
        return password_hash if is_password_correct else None

    async def _get_login_user_for_update(
        self,
        user_id: UUID,
        expected_password_hash: str,
    ) -> User | None:
        """
        Получить юзера для логина, поставив лок на него,
        чтобы конкурентно несколько инстансов не могли менять одного и того же пользака
        """
        locked_user = await self._user_repo.get_for_update(user_id)
        if (
            locked_user is None
            or not locked_user.can_authenticate
            or locked_user.password_hash != expected_password_hash
        ):
            return None
        return locked_user

    async def _get_token_family_for_update(
        self,
        raw_refresh_token: str,
    ) -> _LockedTokenFamily:
        refresh_token_digest = self._refresh_token_codec.digest(raw_refresh_token)
        refresh_token = await self._refresh_token_repo.get_by_hash_for_update(
            refresh_token_digest
        )
        if refresh_token is None:
            raise DomainErrors.Token.INVALID_REFRESH()

        auth_session = await self._auth_session_repo.get_for_update(
            refresh_token.session_id
        )
        if auth_session is None:
            # orphan refresh-запись не должна раскрывать состояние хранилища.
            raise DomainErrors.Token.INVALID_REFRESH()

        return _LockedTokenFamily(
            refresh_token=refresh_token,
            auth_session=auth_session,
        )

    def _issue_tokens(
        self,
        user: User,
        now: datetime,
        log: FilteringBoundLogger,
    ) -> _IssuedTokens:
        access_token = self._access_token_issuer.issue(
            AccessPrincipal(user_id=user.id, role=user.role),
            now=now,
        )
        log.debug("access token issued")

        refresh_token = self._refresh_token_codec.issue()
        log.debug("new refresh token issued")

        return _IssuedTokens(
            token_pair=TokenPair(
                access_token=access_token,
                refresh_token=refresh_token.value,
            ),
            refresh_token_digest=refresh_token.digest,
        )

    async def _store_refresh_token(
        self,
        *,
        auth_session_id: UUID,
        refresh_token_digest: bytes,
        log: FilteringBoundLogger,
    ) -> RefreshToken:
        refresh_token = await self._refresh_token_repo.create(
            RefreshToken(
                session_id=auth_session_id,
                token_hash=refresh_token_digest,
            )
        )
        log.debug(
            "new refresh token stored",
        )
        return refresh_token

    async def _revoke_session(
        self,
        auth_session: AuthSession,
        now: datetime,
    ) -> bool:
        if auth_session.revoked_at is not None:
            return False

        auth_session.revoke(now)
        await self._auth_session_repo.update(auth_session)
        return True

    async def register(self, email: NormalizedEmail, password: str) -> User:
        """
        Флоу таков:
            - хешируем пароль в отдельном потоке, не блокируя event loop
            - создаем пользователя через INSERT ON CONFLICT (email) DO NOTHING
                - если репозиторий не вернул юзера -> такой юзер уже есть -> рейзим ошибку
                - если репозиторий вернул юзера -> юзер успешно создан -> передаем созданного юзера дальше
        """
        register_log = logger.bind(operation="register")
        register_log.debug("register started")

        try:
            start = perf_counter()
            register_log.debug("password hashing started")
            password_hash = await self._password_hasher.hash(password)
            register_log.debug(
                "password hashing ended", duration=perf_counter() - start
            )

            user_to_create = User(email=email, password_hash=password_hash)

            async with self._uow:
                created_user = await self._user_repo.create_if_absent(user_to_create)
                if created_user is None:
                    register_log.info("user registration conflict")
                    raise DomainErrors.User.EMAIL_ALREADY_EXISTS()

            register_log.info("user successfully created")
            return created_user
        except DomainError:
            raise
        except Exception:
            register_log.exception("register failed")
            raise

    async def login(self, email: NormalizedEmail, password: str) -> TokenPair:
        login_log = logger.bind(operation="login")
        login_log.debug("login started")

        stage = "user lookup"
        try:
            # Read-транзакция завершается до Argon2. Иначе запросы, ожидающие
            # semaphore хешера, удерживали бы соединения из DB-пула.
            async with self._uow:
                candidate_user = await self._user_repo.get_by_email(email)

            stage = "password verification"
            verified_password_hash = await self._verify_candidate_password(
                candidate_user,
                password,
            )
            if candidate_user is None or verified_password_hash is None:
                reason = (
                    "user unavailable"
                    if candidate_user is None or not candidate_user.can_authenticate
                    else "incorrect password"
                )
                login_log.warning(
                    "login rejected",
                    stage=stage,
                    reason=reason,
                )
                raise DomainErrors.Auth.INVALID_CREDENTIALS()

            # Между lookup и окончанием Argon2 user мог быть отключен, удален
            # или получить новый password hash. Повторная проверка под lock
            # задает короткую и однозначную границу успешного login.
            async with self._uow:
                stage = "user revalidation"
                locked_user = await self._get_login_user_for_update(
                    candidate_user.id,
                    verified_password_hash,
                )
                if locked_user is None:
                    login_log.warning(
                        "login rejected",
                        stage=stage,
                        reason="user state changed",
                    )
                    raise DomainErrors.Auth.INVALID_CREDENTIALS()

                login_log.debug("login user locked")
                now = self._clock()

                stage = "tokens issuance"
                issued_tokens = self._issue_tokens(locked_user, now, login_log)

                stage = "auth session storage"
                idle_expires_at = now + timedelta(
                    seconds=self._settings.AUTH_SESSION_IDLE_TTL_SECONDS
                )
                created_auth_session = await self._auth_session_repo.create(
                    AuthSession(
                        user_id=locked_user.id,
                        idle_expires_at=idle_expires_at,
                    )
                )
                login_log.debug(
                    "auth session stored",
                    idle_expires_at=created_auth_session.idle_expires_at,
                )

                stage = "refresh token storage"
                await self._store_refresh_token(
                    auth_session_id=created_auth_session.id,
                    refresh_token_digest=issued_tokens.refresh_token_digest,
                    log=login_log,
                )

                token_pair = issued_tokens.token_pair

            login_log.debug("login transaction committed")
            login_log.info("login succeeded")
            return token_pair
        except InvalidCredentialsError:
            raise
        except Exception:
            login_log.exception("login failed", stage=stage)
            raise

    async def logout(self, refresh_token: str) -> None:
        """
        Idempotently revoke the token family associated with the refresh token.

        Состояние refresh-токена, сессии и пользователя не мешает отзыву уже
        найденной family. Stateless access-токен продолжает жить до своего exp.
        """
        logout_log = logger.bind(operation="logout")
        logout_log.debug("logout started")

        stage = "token family lookup"
        try:
            async with self._uow:
                try:
                    context = await self._get_token_family_for_update(refresh_token)
                except InvalidRefreshTokenError:
                    logout_log.warning(
                        "logout rejected",
                        stage=stage,
                        reason="token family unavailable",
                    )
                    raise

                stage = "auth session revocation"
                revoked_now = await self._revoke_session(
                    context.auth_session,
                    self._clock(),
                )

                logout_log.debug(
                    (
                        "auth session revoked"
                        if revoked_now
                        else "auth session already revoked"
                    ),
                    revoked_at=context.auth_session.revoked_at,
                )

            logout_log.debug("logout transaction committed")
            logout_log.info(
                "logout succeeded",
                revoked_now=revoked_now,
                revoked_at=context.auth_session.revoked_at,
            )
        except DomainError:
            raise
        except Exception:
            logout_log.exception("logout failed", stage=stage)
            raise

    async def get_current_user(self, access_token: str) -> User:
        get_user_log = logger.bind(operation="get_current_user")
        stage = "access token verification"
        rejection_reason = "invalid access token"

        try:
            claims = self._access_token_verifier.verify(access_token)
            stage = "user lookup"
            async with self._uow:
                user = await self._user_repo.get(claims.user_id)

                if user is None or not user.can_authenticate:
                    rejection_reason = (
                        "user not found" if user is None else "user cannot authenticate"
                    )
                    # Снаружи не раскрываем состояние subject валидного JWT.
                    raise DomainErrors.Token.INVALID()

            get_user_log.info("current user resolved")
            return user
        except InvalidTokenError:
            get_user_log.warning(
                "current user rejected",
                stage=stage,
                reason=rejection_reason,
            )
            raise
        except DomainError:
            raise
        except Exception:
            get_user_log.exception("get current user failed", stage=stage)
            raise
