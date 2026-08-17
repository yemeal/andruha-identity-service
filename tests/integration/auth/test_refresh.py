from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.conftest import IdentityInfrastructure
from tests.integration.helpers import (
    refresh,
    register_and_login,
    safe_error,
    tokens_from_response,
)

from app.core.settings import get_settings
from app.entrypoints.http.main import create_app
from app.infrastructure.database.models import (
    AuthSessionORM,
    IdempotencyRecordORM,
    RefreshTokenORM,
)

pytestmark = pytest.mark.integration


async def _concurrent_refreshes(
    refresh_token: str,
    keys: list[str],
) -> list[httpx.Response]:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            return await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/auth/refresh",
                        headers={
                            "Cookie": f"refresh_token={refresh_token}",
                            "Idempotency-Key": key,
                        },
                    )
                    for key in keys
                )
            )


async def test_refresh_rotation_chain_consumes_each_predecessor_once(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)

    first = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key="rotation-step-one",
    )
    assert first.status_code == 204
    rotated_once = tokens_from_response(first)
    second = refresh(
        identity_client,
        refresh_token=rotated_once.refresh_token,
        idempotency_key="rotation-step-two",
    )
    assert second.status_code == 204
    rotated_twice = tokens_from_response(second)

    assert (
        len(
            {
                initial.refresh_token,
                rotated_once.refresh_token,
                rotated_twice.refresh_token,
            }
        )
        == 3
    )
    rows = list(
        await database_session.scalars(
            select(RefreshTokenORM).order_by(RefreshTokenORM.created_at)
        )
    )
    assert len(rows) == 3
    assert sum(row.used_at is not None for row in rows) == 2
    assert rows[-1].used_at is None


async def test_same_key_retry_returns_exact_committed_token_pair_once(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)
    key = "lost-response-retry"

    first = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )
    second = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )

    assert first.status_code == second.status_code == 204
    assert tokens_from_response(first) == tokens_from_response(second)
    assert (
        await database_session.scalar(select(func.count()).select_from(RefreshTokenORM))
        == 2
    )
    assert (
        await database_session.scalar(
            select(func.count()).select_from(IdempotencyRecordORM)
        )
        == 1
    )


def test_same_key_with_different_refresh_token_is_conflict(
    identity_client: TestClient,
) -> None:
    _email_a, first_family = register_and_login(identity_client)
    _email_b, second_family = register_and_login(identity_client)
    key = "same-key-different-token"
    assert (
        refresh(
            identity_client,
            refresh_token=first_family.refresh_token,
            idempotency_key=key,
        ).status_code
        == 204
    )

    conflict = refresh(
        identity_client,
        refresh_token=second_family.refresh_token,
        idempotency_key=key,
    )

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "auth.idempotency_key_conflict"


def test_unknown_malformed_and_missing_refresh_are_same_public_failure(
    identity_client: TestClient,
) -> None:
    unknown = refresh(
        identity_client,
        refresh_token="unknown-refresh-token",
        idempotency_key="unknown-refresh",
    )
    malformed = refresh(
        identity_client,
        refresh_token="not-a-valid-token",
        idempotency_key="malformed-refresh",
    )
    identity_client.cookies.clear()
    missing = identity_client.post(
        "/api/v1/auth/refresh",
        headers={"Idempotency-Key": "missing-refresh"},
    )

    assert unknown.status_code == malformed.status_code == missing.status_code == 401
    assert unknown.json() == malformed.json() == missing.json()


async def test_revoked_session_cannot_refresh(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, tokens = register_and_login(identity_client)
    await database_session.execute(
        update(AuthSessionORM).values(revoked_at=datetime.now(UTC))
    )
    await database_session.commit()

    response = refresh(
        identity_client,
        refresh_token=tokens.refresh_token,
        idempotency_key="revoked-session",
    )

    safe_error(response, 401, "auth.invalid_refresh_token")


async def test_reusing_rotated_token_revokes_entire_family(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)
    first = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key="legitimate-rotation",
    )
    replacement = tokens_from_response(first)

    replay = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key="attacker-replay",
    )
    after_replay = refresh(
        identity_client,
        refresh_token=replacement.refresh_token,
        idempotency_key="replacement-after-replay",
    )

    safe_error(replay, 401, "auth.invalid_refresh_token")
    safe_error(after_replay, 401, "auth.invalid_refresh_token")
    revoked_at = await database_session.scalar(select(AuthSessionORM.revoked_at))
    assert revoked_at is not None


@pytest.mark.race
async def test_concurrent_same_key_has_one_rotation_and_no_family_branch(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)

    responses = await _concurrent_refreshes(
        initial.refresh_token,
        ["concurrent-same-key"] * 100,
    )

    statuses = [response.status_code for response in responses]
    assert statuses.count(204) >= 1
    assert set(statuses) <= {204, 423}
    successful_pairs = {
        tokens_from_response(response)
        for response in responses
        if response.status_code == 204
    }
    assert len(successful_pairs) == 1
    assert (
        await database_session.scalar(select(func.count()).select_from(RefreshTokenORM))
        == 2
    )
    assert (
        await database_session.scalar(
            select(func.count()).select_from(IdempotencyRecordORM)
        )
        == 1
    )


@pytest.mark.race
async def test_concurrent_different_keys_detect_replay_and_revoke_winner(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)

    responses = await _concurrent_refreshes(
        initial.refresh_token,
        [f"concurrent-different-{uuid4()}" for _ in range(100)],
    )

    successful = [response for response in responses if response.status_code == 204]
    assert len(successful) <= 1
    assert sum(response.status_code == 401 for response in responses) >= 99
    assert set(response.status_code for response in responses) <= {204, 401}
    assert (
        await database_session.scalar(select(func.count()).select_from(RefreshTokenORM))
        == 2
    )
    assert await database_session.scalar(select(AuthSessionORM.revoked_at)) is not None
    if successful:
        replacement = tokens_from_response(successful[0])
        rejected = refresh(
            identity_client,
            refresh_token=replacement.refresh_token,
            idempotency_key="winner-after-replay-race",
        )
        assert rejected.status_code == 401


@pytest.mark.failure_path
def test_refresh_falls_back_to_postgres_when_valkey_is_unavailable(
    identity_client: TestClient,
    identity_infrastructure: IdentityInfrastructure,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _email, initial = register_and_login(identity_client)
    monkeypatch.setenv("VALKEY_HOST", "127.0.0.1")
    monkeypatch.setenv("VALKEY_PORT", "1")
    monkeypatch.setenv("IDEMPOTENCY_CB_FAILURES", "1")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), base_url="https://testserver") as degraded_client:
            response = refresh(
                degraded_client,
                refresh_token=initial.refresh_token,
                idempotency_key="valkey-down-durable",
            )
        assert response.status_code == 204
    finally:
        get_settings.cache_clear()
