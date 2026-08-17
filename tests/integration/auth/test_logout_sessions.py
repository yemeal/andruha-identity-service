from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import (
    login,
    refresh,
    register_and_login,
    tokens_from_response,
)

from app.entrypoints.http.main import create_app
from app.infrastructure.database.models import AuthSessionORM, UserORM

pytestmark = pytest.mark.integration


def _logout(client: TestClient, refresh_token: str | None = None):
    headers = (
        {} if refresh_token is None else {"Cookie": f"refresh_token={refresh_token}"}
    )
    client.cookies.clear()
    return client.post("/api/v1/auth/logout", headers=headers)


def test_logout_revokes_refresh_and_is_idempotent(identity_client: TestClient) -> None:
    _email, tokens = register_and_login(identity_client)

    first = _logout(identity_client, tokens.refresh_token)
    second = _logout(identity_client, tokens.refresh_token)
    missing = _logout(identity_client)
    refresh_after_logout = refresh(
        identity_client,
        refresh_token=tokens.refresh_token,
        idempotency_key="refresh-after-logout",
    )

    assert first.status_code == second.status_code == missing.status_code == 204
    assert refresh_after_logout.status_code == 401
    assert refresh_after_logout.json()["code"] == "auth.invalid_refresh_token"


async def test_logout_one_device_does_not_revoke_other_sessions(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email, device_a = register_and_login(identity_client)
    second_login = login(identity_client, email=email)
    assert second_login.status_code == 204
    device_b = tokens_from_response(second_login)

    assert _logout(identity_client, device_a.refresh_token).status_code == 204
    rejected_a = refresh(
        identity_client,
        refresh_token=device_a.refresh_token,
        idempotency_key="device-a-after-logout",
    )
    accepted_b = refresh(
        identity_client,
        refresh_token=device_b.refresh_token,
        idempotency_key="device-b-still-active",
    )

    assert rejected_a.status_code == 401
    assert accepted_b.status_code == 204
    user_id = await database_session.scalar(
        select(UserORM.id).where(UserORM.email == email)
    )
    assert user_id is not None
    assert (
        await database_session.scalar(
            select(func.count())
            .select_from(AuthSessionORM)
            .where(
                AuthSessionORM.user_id == user_id,
                AuthSessionORM.revoked_at.is_not(None),
            )
        )
        == 1
    )
    assert (
        await database_session.scalar(
            select(func.count())
            .select_from(AuthSessionORM)
            .where(
                AuthSessionORM.user_id == user_id,
                AuthSessionORM.revoked_at.is_(None),
            )
        )
        == 1
    )


@pytest.mark.race
async def test_concurrent_logout_and_refresh_never_leave_a_live_branch(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            logout_response, refresh_response = await asyncio.gather(
                client.post(
                    "/api/v1/auth/logout",
                    headers={"Cookie": f"refresh_token={initial.refresh_token}"},
                ),
                client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Cookie": f"refresh_token={initial.refresh_token}",
                        "Idempotency-Key": "logout-refresh-race",
                    },
                ),
            )

    assert logout_response.status_code == 204
    assert refresh_response.status_code in {204, 401}
    assert await database_session.scalar(select(AuthSessionORM.revoked_at)) is not None
    if refresh_response.status_code == 204:
        replacement = tokens_from_response(refresh_response)
        rejected = refresh(
            identity_client,
            refresh_token=replacement.refresh_token,
            idempotency_key="post-race-replacement",
        )
        assert rejected.status_code == 401


@pytest.mark.race
async def test_concurrent_logout_requests_are_both_idempotent(
    identity_client: TestClient,
) -> None:
    _email, tokens = register_and_login(identity_client)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/auth/logout",
                        headers={"Cookie": f"refresh_token={tokens.refresh_token}"},
                    )
                    for _ in range(10)
                )
            )

    assert [response.status_code for response in responses] == [204] * 10
