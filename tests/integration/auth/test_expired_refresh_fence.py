import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import (
    refresh,
    register_and_login,
    safe_error,
    tokens_from_response,
)

from app.entrypoints.http.main import create_app
from app.infrastructure.database.models import (
    AuthSessionORM,
    IdempotencyRecordORM,
    RefreshTokenORM,
)

pytestmark = pytest.mark.integration


async def test_expired_refresh_result_preserves_reuse_revocation_without_cleanup(
    database_session: AsyncSession,
    valkey_client: Redis,
) -> None:
    with TestClient(
        create_app(), base_url="https://testserver", raise_server_exceptions=False
    ) as client:
        _, initial = register_and_login(client)
        first = refresh(
            client,
            refresh_token=initial.refresh_token,
            idempotency_key="expired-refresh-fence",
        )
        assert first.status_code == 204
        replacement = tokens_from_response(first)

        now = datetime.now(UTC)
        await database_session.execute(
            update(IdempotencyRecordORM).values(
                created_at=now - timedelta(hours=1),
                completed_at=now - timedelta(hours=1),
                expires_at=now - timedelta(seconds=1),
            )
        )
        await database_session.commit()
        await valkey_client.flushdb()

        replay = refresh(
            client,
            refresh_token=initial.refresh_token,
            idempotency_key="expired-refresh-fence",
        )
        safe_error(replay, 401, "auth.invalid_refresh_token")
        assert (
            await database_session.scalar(select(AuthSessionORM.revoked_at)) is not None
        )
        assert (
            await database_session.scalar(
                select(func.count()).select_from(RefreshTokenORM)
            )
            == 2
        )
        rejected = refresh(
            client,
            refresh_token=replacement.refresh_token,
            idempotency_key="replacement-after-expired-fence",
        )
        safe_error(rejected, 401, "auth.invalid_refresh_token")


@pytest.mark.race
async def test_concurrent_expired_refresh_retries_do_not_branch_the_family(
    database_session: AsyncSession,
    valkey_client: Redis,
) -> None:
    with TestClient(create_app(), base_url="https://testserver") as client:
        _, initial = register_and_login(client)
        first = refresh(
            client,
            refresh_token=initial.refresh_token,
            idempotency_key="expired-refresh-race",
        )
        assert first.status_code == 204

    now = datetime.now(UTC)
    await database_session.execute(
        update(IdempotencyRecordORM).values(
            created_at=now - timedelta(hours=1),
            completed_at=now - timedelta(hours=1),
            expires_at=now - timedelta(seconds=1),
        )
    )
    await database_session.commit()
    await valkey_client.flushdb()

    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        ) as client,
    ):
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Cookie": f"refresh_token={initial.refresh_token}",
                        "Idempotency-Key": "expired-refresh-race",
                    },
                )
                for _ in range(32)
            )
        )

    statuses = [response.status_code for response in responses]
    assert 401 in statuses
    assert set(statuses) <= {401, 423}
    assert await database_session.scalar(select(AuthSessionORM.revoked_at)) is not None
    assert (
        await database_session.scalar(select(func.count()).select_from(RefreshTokenORM))
        == 2
    )
