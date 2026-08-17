from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.auth_sessions import AuthSession
from app.domain.exceptions import (
    AuthSessionInactiveError,
    InvalidRefreshTokenError,
    InvalidTokenError,
)
from app.domain.refresh_tokens import RefreshToken


class TestAuthSessionIdleExpiration:
    def test_successful_refresh_extends_same_session_from_now(self) -> None:
        """
        Проверяем: скользящее продление активной auth-сессии.
        Успех: меняется только idle_expires_at той же сессии.
        Нежелательное поведение: ротация создает новую сессию или абсолютный дедлайн.
        """
        now = datetime(2026, 7, 24, 12, tzinfo=UTC)
        idle_ttl = timedelta(days=30)
        session = AuthSession(
            user_id=uuid4(),
            idle_expires_at=now + timedelta(days=1),
        )
        session_id = session.id

        session.extend_idle(now, idle_ttl)

        assert session.id == session_id
        assert session.idle_expires_at == now + idle_ttl
        assert "absolute_expires_at" not in AuthSession.model_fields

    @pytest.mark.parametrize("revoked", [False, True])
    def test_inactive_session_cannot_be_extended(self, revoked: bool) -> None:
        """
        Проверяем: запрет оживления истекшей или отозванной сессии.
        Успех: оба состояния сообщают доменную ошибку невалидного refresh-токена.
        Нежелательное поведение: ротация возвращает неактивную сессию к жизни.
        """
        now = datetime(2026, 7, 24, 12, tzinfo=UTC)
        session = AuthSession(
            user_id=uuid4(),
            idle_expires_at=(
                now + timedelta(days=1) if revoked else now - timedelta(seconds=1)
            ),
            revoked_at=now if revoked else None,
        )
        old_deadline = session.idle_expires_at

        with pytest.raises(AuthSessionInactiveError) as captured:
            session.extend_idle(now, timedelta(days=30))

        assert isinstance(captured.value, InvalidRefreshTokenError)
        assert isinstance(captured.value, InvalidTokenError)
        assert session.idle_expires_at == old_deadline


class TestRefreshTokenLifetime:
    def test_refresh_token_has_no_own_expiration(self) -> None:
        """
        Проверяем: контракт срока жизни одноразового refresh-токена.
        Успех: токен хранит session_id и used_at, но не собственный expires_at.
        Нежелательное поведение: срок токена расходится со сроком связанной сессии.
        """
        token = RefreshToken(
            session_id=uuid4(),
            token_hash=b"\0" * 32,
        )

        assert token.used_at is None
        assert "expires_at" not in RefreshToken.model_fields
        assert "idle_expires_at" not in RefreshToken.model_fields
