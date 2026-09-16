from datetime import datetime, timedelta
from typing import Self
from uuid import UUID, uuid7

from pydantic import Field, model_validator

from app.domain.base import Entity, require_aware
from app.domain.entities.refresh_token import RefreshToken
from app.domain.exceptions import (
    AuthSessionInactiveError,
    InvalidDomainStateError,
    InvalidRefreshTokenError,
    InvalidSessionLifetimeError,
    InvalidTimestampError,
    RefreshTokenReuseError,
)


class AuthSession(Entity):
    """Корень сессии с предъявленным токеном и результатом его ротации.

    История токенов хранится в репозитории. Для одного перехода нужны только
    предъявленный токен и сессия; replacement сохраняется в той же транзакции.
    """

    user_id: UUID
    idle_expires_at: datetime
    revoked_at: datetime | None = None
    refresh_token: RefreshToken | None = Field(default=None, exclude=True, repr=False)
    replacement_token: RefreshToken | None = Field(
        default=None, exclude=True, repr=False
    )

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        require_aware(self.idle_expires_at)
        if self.idle_expires_at <= self.created_at:
            raise InvalidTimestampError()
        if self.revoked_at is not None:
            require_aware(self.revoked_at)
            if self.revoked_at < self.created_at:
                raise InvalidTimestampError()
        for token in (self.refresh_token, self.replacement_token):
            if token is not None and (
                token.session_id != self.id or token.created_at < self.created_at
            ):
                raise InvalidDomainStateError()
        if self.replacement_token is not None and (
            self.refresh_token is None
            or not self.refresh_token.is_used
            or self.replacement_token.is_used
            or self.replacement_token.id == self.refresh_token.id
            or self.replacement_token.token_hash == self.refresh_token.token_hash
        ):
            raise InvalidDomainStateError()
        return self

    @classmethod
    def start(
        cls, *, user_id: UUID, token_hash: bytes, now: datetime, idle_ttl: timedelta
    ) -> Self:
        cls._validate_ttl(idle_ttl)
        session_id = uuid7()
        return cls(
            id=session_id,
            user_id=user_id,
            created_at=now,
            idle_expires_at=now + idle_ttl,
            refresh_token=RefreshToken(
                session_id=session_id, token_hash=token_hash, created_at=now
            ),
        )

    @staticmethod
    def _validate_ttl(idle_ttl: timedelta) -> None:
        if idle_ttl <= timedelta(0):
            raise InvalidSessionLifetimeError()

    def is_active(self, now: datetime) -> bool:
        require_aware(now)
        return self.revoked_at is None and self.created_at <= now < self.idle_expires_at

    def accepts_refresh(self, *, now: datetime, user_can_authenticate: bool) -> bool:
        """Возвращает отказ с сохранённым отзывом при reuse/отключённом аккаунте."""
        token = self._require_token()
        if token.is_used:
            self.revoke(now)
            return False
        if not self.is_active(now):
            raise AuthSessionInactiveError()
        if not user_can_authenticate:
            self.revoke(now)
            return False
        return True

    def rotate(
        self, *, now: datetime, idle_ttl: timedelta, token_hash: bytes
    ) -> RefreshToken:
        self._validate_ttl(idle_ttl)
        if not self.is_active(now):
            raise AuthSessionInactiveError()
        token = self._require_token()
        if token.is_used:
            raise RefreshTokenReuseError()
        replacement = RefreshToken(
            session_id=self.id, token_hash=token_hash, created_at=now
        )
        self._change_state(
            refresh_token=token._consumed(now),
            replacement_token=replacement,
            idle_expires_at=now + idle_ttl,
        )
        return replacement

    def can_replay(self, now: datetime) -> bool:
        return not self._require_token().is_used and self.is_active(now)

    def revoke(self, now: datetime) -> None:
        if self.revoked_at is None:
            self._change_state(revoked_at=now)

    def _require_token(self) -> RefreshToken:
        if self.refresh_token is None:
            raise InvalidRefreshTokenError()
        return self.refresh_token
