from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import (
    refresh,
    register_and_login,
    tokens_from_response,
)

from app.core.settings import get_settings
from app.entrypoints.http.main import create_app
from app.infrastructure.database.models import (
    AuthSessionORM,
    IdempotencyRecordORM,
    RefreshTokenORM,
)

pytestmark = [pytest.mark.integration, pytest.mark.failure_path]


def test_real_readiness_reports_both_live_dependencies(
    identity_client: TestClient,
) -> None:
    response = identity_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "postgres": "ok",
        "valkey": "ok",
    }


def test_postgres_unavailable_returns_safe_5xx_and_failed_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_HOST", "127.0.0.1")
    monkeypatch.setenv("DATABASE_PORT", "1")
    get_settings.cache_clear()
    try:
        with TestClient(
            create_app(),
            base_url="https://testserver",
            raise_server_exceptions=False,
        ) as client:
            readiness = client.get("/health/ready")
            register = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "postgres-down@example.com",
                    "password": "Integration-password-123",
                },
            )
    finally:
        get_settings.cache_clear()

    assert readiness.status_code == 503
    assert readiness.json()["postgres"] == "unavailable"
    assert register.status_code == 500
    assert register.json() == {
        "code": "request.internal_error",
        "detail": "internal server error",
    }
    assert "postgres-down@example.com" not in register.text


async def test_corrupted_durable_replay_fails_closed_without_second_rotation(
    identity_client: TestClient,
    database_session: AsyncSession,
    valkey_client: Redis,
) -> None:
    _email, initial = register_and_login(identity_client)
    key = "corrupted-replay"
    first = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )
    assert first.status_code == 204
    record = await database_session.scalar(select(IdempotencyRecordORM))
    assert record is not None and record.result_payload is not None
    corrupted = dict(record.result_payload)
    corrupted["ciphertext"] = "AAAA"
    await database_session.execute(
        update(IdempotencyRecordORM)
        .where(IdempotencyRecordORM.id == record.id)
        .values(result_payload=corrupted)
    )
    await database_session.commit()
    await valkey_client.flushdb()

    replay = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )

    assert replay.status_code == 503
    assert replay.json()["code"] == "auth.refresh_replay_unavailable"
    assert "set-cookie" not in replay.headers
    assert (
        await database_session.scalar(select(func.count()).select_from(RefreshTokenORM))
        == 2
    )


async def test_replay_is_rejected_after_replacement_session_is_revoked(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)
    key = "stale-replay-after-logout"
    first = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )
    assert first.status_code == 204
    replacement = tokens_from_response(first)
    identity_client.cookies.clear()
    logout = identity_client.post(
        "/api/v1/auth/logout",
        headers={"Cookie": f"refresh_token={replacement.refresh_token}"},
    )
    assert logout.status_code == 204

    replay = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )

    assert replay.status_code == 401
    assert replay.json()["code"] == "auth.invalid_refresh_token"
    assert await database_session.scalar(select(AuthSessionORM.revoked_at)) is not None


async def test_expired_session_cannot_be_revived_by_refresh(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)
    created_at = await database_session.scalar(select(AuthSessionORM.created_at))
    assert created_at is not None
    await database_session.execute(
        update(AuthSessionORM).values(
            idle_expires_at=max(
                created_at + timedelta(microseconds=1),
                datetime.now(UTC) - timedelta(microseconds=1),
            )
        )
    )
    await database_session.commit()

    response = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key="expired-session",
    )

    assert response.status_code == 401
    assert response.json()["code"] == "auth.invalid_refresh_token"
