from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.application.exceptions.idempotency import (
    IdempotencyStorageUnavailableError,
)
from app.application.ports.dto.idempotency import CompletedIdempotencyResult
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import BeginAction, IdempotencyIdentity
from app.infrastructure.cache.valkey_idempotency_store import (
    ValkeyHotIdempotencyStore,
)

pytestmark = pytest.mark.integration


def _identity() -> IdempotencyIdentity:
    return IdempotencyIdentity(
        subject_id="public-refresh",
        operation="auth.refresh",
        key_hash=hash_idempotency_key(f"integration-{uuid4()}"),
    )


def _request_hash() -> bytes:
    return compute_request_hash({"refresh_token_digest": uuid4().hex})


def _completed(request_hash: bytes) -> CompletedIdempotencyResult:
    return CompletedIdempotencyResult(
        request_hash=request_hash,
        result_type="auth.refresh.success",
        result_payload={
            "version": 1,
            "algorithm": "AES-256-GCM",
            "key_id": "test",
            "nonce": "nonce",
            "ciphertext": "ciphertext",
        },
        resource_type="refresh_token",
        resource_id=uuid4(),
    )


async def _single_key(client: Redis, namespace: str) -> bytes:
    keys = await client.keys(f"{namespace}:*")
    assert len(keys) == 1
    return keys[0]


async def test_processing_lock_is_atomic_owned_and_has_ttl(
    valkey_client: Redis,
) -> None:
    namespace = f"identity-test:{uuid4()}"
    store = ValkeyHotIdempotencyStore(
        valkey_client,
        result_ttl_seconds=5,
        key_namespace=namespace,
    )
    identity = _identity()
    request_hash = _request_hash()
    owner = uuid4()

    acquired = await store.begin(identity, request_hash, owner, 2)
    duplicate = await store.begin(identity, request_hash, uuid4(), 2)
    conflict = await store.begin(identity, _request_hash(), uuid4(), 2)
    key = await _single_key(valkey_client, namespace)

    assert acquired.action is BeginAction.ACQUIRED
    assert duplicate.action is BeginAction.IN_PROGRESS
    assert conflict.action is BeginAction.CONFLICT
    assert 0 < await valkey_client.ttl(key) <= 2
    assert await store.renew(identity, uuid4(), 5) is False
    assert await store.renew(identity, owner, 5) is True
    assert 0 < await valkey_client.ttl(key) <= 5


async def test_complete_uses_owner_cas_and_replays_cached_result(
    valkey_client: Redis,
) -> None:
    namespace = f"identity-test:{uuid4()}"
    store = ValkeyHotIdempotencyStore(
        valkey_client,
        result_ttl_seconds=7,
        key_namespace=namespace,
    )
    identity = _identity()
    request_hash = _request_hash()
    owner = uuid4()
    completed = _completed(request_hash)
    assert (
        await store.begin(identity, request_hash, owner, 2)
    ).action is BeginAction.ACQUIRED

    assert await store.complete(identity, uuid4(), completed) is False
    assert await store.complete(identity, owner, completed) is True
    replay = await store.begin(identity, request_hash, uuid4(), 2)
    key = await _single_key(valkey_client, namespace)

    assert replay.action is BeginAction.REPLAY
    assert replay.completed == completed
    assert 0 < await valkey_client.ttl(key) <= 7
    assert await store.abandon(identity, owner) is False
    assert await valkey_client.exists(key) == 1


async def test_abandon_only_removes_processing_lock_owned_by_caller(
    valkey_client: Redis,
) -> None:
    namespace = f"identity-test:{uuid4()}"
    store = ValkeyHotIdempotencyStore(
        valkey_client,
        result_ttl_seconds=5,
        key_namespace=namespace,
    )
    identity = _identity()
    request_hash = _request_hash()
    owner = uuid4()
    assert (
        await store.begin(identity, request_hash, owner, 2)
    ).action is BeginAction.ACQUIRED

    assert await store.abandon(identity, uuid4()) is False
    assert await store.abandon(identity, owner) is True
    assert (
        await store.begin(identity, request_hash, uuid4(), 2)
    ).action is BeginAction.ACQUIRED


async def test_crashed_owner_lock_expires_and_can_be_reacquired(
    valkey_client: Redis,
) -> None:
    namespace = f"identity-test:{uuid4()}"
    store = ValkeyHotIdempotencyStore(
        valkey_client,
        result_ttl_seconds=5,
        key_namespace=namespace,
    )
    identity = _identity()
    request_hash = _request_hash()
    assert (
        await store.begin(identity, request_hash, uuid4(), 1)
    ).action is BeginAction.ACQUIRED

    await asyncio.sleep(1.1)

    assert (
        await store.begin(identity, request_hash, uuid4(), 1)
    ).action is BeginAction.ACQUIRED


@pytest.mark.race
async def test_one_of_one_hundred_clients_acquires_same_lock(
    valkey_client: Redis,
) -> None:
    namespace = f"identity-test:{uuid4()}"
    store = ValkeyHotIdempotencyStore(
        valkey_client,
        result_ttl_seconds=5,
        key_namespace=namespace,
    )
    identity = _identity()
    request_hash = _request_hash()

    results = await asyncio.gather(
        *(store.begin(identity, request_hash, uuid4(), 5) for _ in range(100))
    )

    assert sum(result.action is BeginAction.ACQUIRED for result in results) == 1
    assert sum(result.action is BeginAction.IN_PROGRESS for result in results) == 99


async def test_corrupted_entry_fails_closed(
    valkey_client: Redis,
) -> None:
    namespace = f"identity-test:{uuid4()}"
    store = ValkeyHotIdempotencyStore(
        valkey_client,
        result_ttl_seconds=5,
        key_namespace=namespace,
    )
    identity = _identity()
    request_hash = _request_hash()
    owner = uuid4()
    assert (
        await store.begin(identity, request_hash, owner, 5)
    ).action is BeginAction.ACQUIRED
    key = await _single_key(valkey_client, namespace)
    await valkey_client.hdel(key, "format_version")

    with pytest.raises(IdempotencyStorageUnavailableError):
        await store.begin(identity, request_hash, uuid4(), 5)
