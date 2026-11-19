from uuid import uuid7

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from tests.integration.helpers import register

from app.infrastructure.database.models import UserORM
from app.core.settings import get_settings
from app.entrypoints.http.main import create_app


@pytest.fixture
def lookup_client(clean_identity_state, monkeypatch):
    monkeypatch.setenv("IDENTITY_INTERNAL_API_TOKEN", "internal-lookup-test-token")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client


async def test_internal_lookup_returns_only_completed_active_account_ids(
    lookup_client: TestClient, database_session
):
    identity_client = lookup_client
    _, completed = register(identity_client)
    assert completed.status_code == 201
    active_id = completed.json()["userId"]
    _, completed_disabled = register(identity_client)
    disabled_id = completed_disabled.json()["userId"]
    await database_session.execute(
        update(UserORM).where(UserORM.id == disabled_id).values(status="DISABLED")
    )
    await database_session.commit()
    absent_id = str(uuid7())

    response = identity_client.post(
        "/internal/v1/accounts/lookup",
        headers={"X-Service-Token": "internal-lookup-test-token"},
        json={"userIds": [active_id, disabled_id, absent_id, active_id]},
    )

    assert response.status_code == 200
    assert response.json() == {"registeredUserIds": [active_id]}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("credential", [None, "incorrect-service-token"])
def test_internal_lookup_requires_service_credential(lookup_client, credential):
    response = lookup_client.post(
        "/internal/v1/accounts/lookup",
        headers={"X-Service-Token": credential} if credential else {},
        json={"userIds": [str(uuid7())]},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("size", [0, 101])
def test_internal_lookup_rejects_unbounded_or_empty_batch(lookup_client, size):
    response = lookup_client.post(
        "/internal/v1/accounts/lookup",
        headers={"X-Service-Token": "internal-lookup-test-token"},
        json={"userIds": [str(uuid7()) for _ in range(size)]},
    )
    assert response.status_code == 422


def test_pending_registration_is_not_a_registered_account(lookup_client, profile_peer):
    profile_peer.status = 503
    _, pending = register(lookup_client)
    assert pending.status_code == 202
    user_id = pending.json()["userId"]
    response = lookup_client.post(
        "/internal/v1/accounts/lookup",
        headers={"X-Service-Token": "internal-lookup-test-token"},
        json={"userIds": [user_id]},
    )
    assert response.status_code == 200
    assert response.json() == {"registeredUserIds": []}
