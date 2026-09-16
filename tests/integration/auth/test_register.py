from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import DEFAULT_PASSWORD, register, unique_email

from app.infrastructure.database.models import (
    OutboxMessageORM,
    RegistrationOperationORM,
    UserORM,
)
from tests.integration.profile_server import ProfilePeer

from app.application.services.registration_reconciler import (
    RegistrationReconcilerProtocol,
)
from app.core.settings import get_settings
from app.infrastructure.di import create_container

pytestmark = pytest.mark.integration


async def test_register_waits_for_profile_and_rolls_back_on_failure(
    identity_client: TestClient,
    database_session: AsyncSession,
    profile_peer: ProfilePeer,
) -> None:
    profile_peer.status = 503
    email = unique_email("profile-unavailable")
    idempotency_key = "profile-unavailable-key"
    _, response = register(
        identity_client,
        email=email,
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert await database_session.scalar(select(func.count()).select_from(UserORM)) == 0
    assert (
        await database_session.scalar(
            select(func.count()).select_from(OutboxMessageORM)
        )
        == 0
    )
    assert len(profile_peer.requests) == 2
    assert profile_peer.requests[0] == profile_peer.requests[1]
    assert (
        await database_session.scalar(
            select(func.count()).select_from(RegistrationOperationORM)
        )
        == 1
    )

    profile_peer.status = 204
    await database_session.execute(
        update(RegistrationOperationORM).values(
            available_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    )
    await database_session.commit()
    _, response = register(
        identity_client,
        email=email,
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 201
    assert await database_session.scalar(select(func.count()).select_from(UserORM)) == 1
    assert (
        await database_session.scalar(
            select(func.count()).select_from(OutboxMessageORM)
        )
        == 1
    )


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
    operation = await database_session.scalar(select(RegistrationOperationORM))
    assert operation is not None
    assert operation.status.value == "COMPLETED"
    assert operation.password_hash is None


async def test_register_replays_completed_result_with_same_key(
    identity_client: TestClient,
    profile_peer: ProfilePeer,
) -> None:
    email = unique_email("registration-replay")
    key = "registration-replay-key"

    _, first = register(identity_client, email=email, idempotency_key=key)
    _, replay = register(identity_client, email=email, idempotency_key=key)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert len(profile_peer.requests) == 1


async def test_register_rejects_same_key_with_different_password(
    identity_client: TestClient,
) -> None:
    email = unique_email("registration-key-conflict")
    key = "registration-conflict-key"
    _, first = register(identity_client, email=email, idempotency_key=key)
    _, conflict = register(
        identity_client,
        email=email,
        password="Different-password-123",
        idempotency_key=key,
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "auth.idempotency_key_conflict"


async def test_reconciler_completes_registration_after_sync_profile_failure(
    identity_client: TestClient,
    database_session: AsyncSession,
    profile_peer: ProfilePeer,
) -> None:
    profile_peer.status = 503
    email = unique_email("reconciler")
    _, response = register(
        identity_client,
        email=email,
        idempotency_key="reconciler-registration-key",
    )
    assert response.status_code == 202
    registration_id = response.json()["registrationId"]

    await database_session.execute(
        update(RegistrationOperationORM).values(
            available_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    )
    await database_session.commit()
    profile_peer.status = 204

    get_settings.cache_clear()
    container = create_container()
    try:
        reconciler = await container.get(RegistrationReconcilerProtocol)
        assert await reconciler.run_once() == 1
    finally:
        await container.close()
        get_settings.cache_clear()

    operation = await database_session.get(
        RegistrationOperationORM,
        registration_id,
        populate_existing=True,
    )
    assert operation is not None
    assert operation.status.value == "COMPLETED"
    assert operation.password_hash is None
    assert await database_session.scalar(select(func.count()).select_from(UserORM)) == 1
    assert (
        await database_session.scalar(
            select(func.count()).select_from(OutboxMessageORM)
        )
        == 1
    )


async def test_register_duplicate_and_case_variant_return_same_conflict(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email = unique_email("duplicate")
    first = identity_client.post(
        "/api/v1/auth/register",
        headers={"Idempotency-Key": "duplicate-first-key"},
        json={"email": email.title(), "password": DEFAULT_PASSWORD},
    )
    duplicate = identity_client.post(
        "/api/v1/auth/register",
        headers={"Idempotency-Key": "duplicate-second-key"},
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
            headers={"Idempotency-Key": f"register-race-key-{_}"},
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
