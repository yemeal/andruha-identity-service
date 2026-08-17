from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import DEFAULT_PASSWORD, register, unique_email

from app.infrastructure.database.models import UserORM

pytestmark = pytest.mark.integration


async def test_register_success_persists_normalized_user_with_argon2_hash(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email = unique_email("register").upper()

    selected_email, response = register(identity_client, email=email)

    assert response.status_code == 201
    user = await database_session.scalar(
        select(UserORM).where(UserORM.email == selected_email.lower())
    )
    assert user is not None
    assert user.email == selected_email.lower()
    assert user.password_hash != DEFAULT_PASSWORD
    assert user.password_hash.startswith("$argon2")
    assert "password" not in response.text.lower()


async def test_register_duplicate_and_case_variant_return_same_conflict(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email = unique_email("duplicate")
    first = identity_client.post(
        "/api/v1/auth/register",
        json={"email": email.title(), "password": DEFAULT_PASSWORD},
    )
    duplicate = identity_client.post(
        "/api/v1/auth/register",
        json={"email": email.upper(), "password": DEFAULT_PASSWORD},
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "auth.email_already_exists"
    assert await database_session.scalar(select(func.count()).select_from(UserORM)) == 1


@pytest.mark.race
async def test_concurrent_same_email_creates_exactly_one_user(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email = unique_email("register-race")

    def attempt(_: int) -> int:
        response = identity_client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": DEFAULT_PASSWORD},
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=20) as executor:
        statuses = list(executor.map(attempt, range(100)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 99
    assert (
        await database_session.scalar(
            select(func.count()).select_from(UserORM).where(UserORM.email == email)
        )
        == 1
    )
