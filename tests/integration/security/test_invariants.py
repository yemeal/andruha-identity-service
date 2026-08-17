from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import (
    me,
    refresh,
    register_and_login,
    tokens_from_response,
)

from app.infrastructure.database.models import IdempotencyRecordORM, UserORM

pytestmark = pytest.mark.integration


async def test_password_and_hash_never_leave_http_api(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email, tokens = register_and_login(identity_client)
    stored_hash = await database_session.scalar(
        select(UserORM.password_hash).where(UserORM.email == email)
    )
    assert stored_hash is not None

    response = me(identity_client, tokens.access_token)

    assert response.status_code == 200
    assert "password" not in response.text.lower()
    assert stored_hash not in response.text


async def test_refresh_success_is_only_an_encrypted_envelope_in_stores(
    identity_client: TestClient,
    database_session: AsyncSession,
    valkey_client: Redis,
) -> None:
    _email, initial = register_and_login(identity_client)
    key = "secret-idempotency-key"

    response = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key=key,
    )

    assert response.status_code == 204
    replacement = tokens_from_response(response)
    record = await database_session.scalar(select(IdempotencyRecordORM))
    assert record is not None
    payload = record.result_payload
    assert payload is not None
    assert payload["algorithm"] == "AES-256-GCM"
    serialized = json.dumps(payload, sort_keys=True)
    assert initial.refresh_token not in serialized
    assert initial.access_token not in serialized
    assert replacement.refresh_token not in serialized
    assert replacement.access_token not in serialized
    assert key not in serialized

    keys = await valkey_client.keys("andruha-identity-integration:idempotency:v1:*")
    assert len(keys) == 1
    raw_key = keys[0]
    raw_values = b"".join((await valkey_client.hgetall(raw_key)).values())
    for secret in (
        key,
        initial.refresh_token,
        initial.access_token,
        replacement.refresh_token,
        replacement.access_token,
    ):
        assert secret.encode() not in raw_key
        assert secret.encode() not in raw_values


async def test_each_refresh_result_uses_a_fresh_aes_gcm_nonce(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    _email, initial = register_and_login(identity_client)
    first = refresh(
        identity_client,
        refresh_token=initial.refresh_token,
        idempotency_key="nonce-rotation-one",
    )
    assert first.status_code == 204
    first_pair = tokens_from_response(first)
    second = refresh(
        identity_client,
        refresh_token=first_pair.refresh_token,
        idempotency_key="nonce-rotation-two",
    )
    assert second.status_code == 204

    payloads = list(
        await database_session.scalars(
            select(IdempotencyRecordORM.result_payload).order_by(
                IdempotencyRecordORM.created_at
            )
        )
    )
    assert len(payloads) == 2
    assert payloads[0] is not None and payloads[1] is not None
    assert payloads[0]["nonce"] != payloads[1]["nonce"]
    assert payloads[0]["ciphertext"] != payloads[1]["ciphertext"]
