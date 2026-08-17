"""
Unit-контракт Redis hot lease/replay adapter.

Fake eval проверяет wire-протокол, коды Lua, CAS и сериализацию без Redis.
Opt-in integration в отдельном файле исполняет те же Lua-скрипты на Redis.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
import redis.exceptions
from redis.asyncio import Redis

from app.application.exceptions.idempotency import (
    IdempotencyStorageUnavailableError,
)
from app.application.ports.dto.idempotency import CompletedIdempotencyResult
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    BeginAction,
    IdempotencyIdentity,
)
from app.infrastructure.cache.valkey_idempotency_store import (
    ValkeyHotIdempotencyStore,
)


@dataclass
class FakeEvalRedis:
    responses: list[Any]
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def eval(self, *arguments: Any) -> Any:
        self.calls.append(arguments)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _identity(
    *,
    subject_id: str = "customer@example.com",
    operation: str = "create_order",
    raw_key: str = "secret-client-key",
) -> IdempotencyIdentity:
    return IdempotencyIdentity(
        subject_id=subject_id,
        operation=operation,
        key_hash=hash_idempotency_key(raw_key),
    )


def _request_hash(quantity: int = 1) -> bytes:
    return compute_request_hash(
        {"items": [{"productId": "sku-1", "quantity": quantity}]},
        unordered_paths={("items",)},
    )


def _completed(
    request_hash: bytes | None = None,
) -> CompletedIdempotencyResult:
    order_id = uuid.uuid4()
    return CompletedIdempotencyResult(
        request_hash=request_hash or _request_hash(),
        result_type="generic_snapshot",
        result_payload={
            "id": order_id,
            "amount": Decimal("1250.50"),
            "created_at": datetime(2026, 7, 28, 10, 30, tzinfo=UTC),
            "lines": [{"product_id": "sku-1", "quantity": 1}],
        },
        result_version=2,
        resource_type="generic_resource",
        resource_id=order_id,
        resource_version=2,
    )


def _store(
    fake: FakeEvalRedis,
    *,
    result_ttl_seconds: int = 3600,
    namespace: str = "test:order-service:idempotency",
) -> ValkeyHotIdempotencyStore:
    return ValkeyHotIdempotencyStore(
        cast(Redis, fake),
        result_ttl_seconds=result_ttl_seconds,
        key_namespace=namespace,
    )


class TestBeginDecisions:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ([1], BeginAction.ACQUIRED),
            ([3], BeginAction.CONFLICT),
            ([4], BeginAction.IN_PROGRESS),
        ],
    )
    async def test_maps_terminal_script_codes(
        self,
        raw: list[int],
        expected: BeginAction,
    ) -> None:
        """
        Проверяем: begin переводит известные Lua-коды в entrypoint-neutral решение.
        Успех: ACQUIRED, CONFLICT и IN_PROGRESS возвращаются без cached результата.
        Нежелательное поведение: HTTP/Kafka слои вынуждены разбирать Redis-коды.
        """
        fake = FakeEvalRedis([raw])

        result = await _store(fake).begin(
            _identity(),
            _request_hash(),
            uuid.uuid4(),
            60,
        )

        assert result.action is expected
        assert result.completed is None

    async def test_replay_decodes_json_safe_completed_result(self) -> None:
        """
        Проверяем: completed HASH восстанавливается из Redis bytes в общий result.
        Успех: UUID, Decimal и datetime представлены JSON-safe значениями без потерь.
        Нежелательное поведение: replay зависит от Python-объектов в Redis payload.
        """
        completed = _completed()
        encoded = ValkeyHotIdempotencyStore._encode_completed(completed)
        fake = FakeEvalRedis([[2, encoded.encode("utf-8")]])

        result = await _store(fake).begin(
            _identity(),
            completed.request_hash,
            uuid.uuid4(),
            60,
        )

        assert result.action is BeginAction.REPLAY
        assert result.completed is not None
        assert result.completed.request_hash == completed.request_hash
        assert result.completed.result_payload == {
            "id": str(completed.result_payload["id"]),
            "amount": "1250.50",
            "created_at": "2026-07-28T10:30:00Z",
            "lines": [{"product_id": "sku-1", "quantity": 1}],
        }
        assert str(result.completed.resource_id) == str(completed.resource_id)
        assert result.completed.resource_version == 2

    @pytest.mark.parametrize(
        "raw",
        [
            [5],
            [99],
            [],
            "1",
            [b"not-a-code"],
            [2],
            [2, None],
            [2, b"not-json"],
            [2, b"\xff"],
            [2, json.dumps({"request_hash": "zz"})],
            [
                2,
                ValkeyHotIdempotencyStore._encode_completed(
                    _completed(_request_hash(quantity=2))
                ),
            ],
        ],
    )
    async def test_corrupted_cache_result_uses_durable_fallback_error(
        self,
        raw: Any,
    ) -> None:
        """
        Проверяем: повреждённый Lua/HASH result не становится валидным replay.
        Успех: corruption даёт StorageUnavailable для authoritative DB fallback.
        Нежелательное поведение: optional Redis cache вызывает 500 или false success.
        """
        fake = FakeEvalRedis([raw])

        with pytest.raises(IdempotencyStorageUnavailableError):
            await _store(fake).begin(
                _identity(),
                _request_hash(),
                uuid.uuid4(),
                60,
            )


class TestLuaCASAndTTL:
    @pytest.mark.parametrize(
        ("script_name", "argument_count"),
        [
            ("_BEGIN_SCRIPT", 3),
            ("_RENEW_SCRIPT", 2),
            ("_COMPLETE_SCRIPT", 4),
            ("_ABANDON_SCRIPT", 1),
        ],
    )
    def test_script_references_only_supplied_arguments(
        self,
        script_name: str,
        argument_count: int,
    ) -> None:
        """
        Проверяем: Lua не читает отсутствующий ARGV и не делает CAS всегда false.
        Успех: максимальный индекс ARGV не превышает число переданных аргументов.
        Нежелательное поведение: heartbeat живого owner никогда не продлевает lease.
        """
        script = getattr(ValkeyHotIdempotencyStore, script_name)
        indexes = [int(value) for value in re.findall(r"ARGV\[(\d+)\]", script)]

        assert indexes
        assert max(indexes) <= argument_count

    @pytest.mark.parametrize("result", [0, 1])
    async def test_renew_returns_owner_cas_decision_and_lease_ttl(
        self,
        result: int,
    ) -> None:
        """
        Проверяем: renew возвращает Lua CAS и передаёт новый lease TTL.
        Успех: 1/0 становится True/False, owner и 90 секунд доходят до eval.
        Нежелательное поведение: stale worker продлевает lease другого owner.
        """
        owner = uuid.uuid4()
        fake = FakeEvalRedis([result])

        renewed = await _store(fake).renew(
            _identity(),
            owner,
            lease_seconds=90,
        )

        assert renewed is bool(result)
        assert fake.calls[0][3:] == (str(owner), "90")
        assert "state == 'processing'" in fake.calls[0][0]
        assert "owner == ARGV[1]" in fake.calls[0][0]

    @pytest.mark.parametrize("result", [0, 1])
    async def test_complete_returns_owner_cas_and_serializes_result(
        self,
        result: int,
    ) -> None:
        """
        Проверяем: complete атомарно меняет только lease текущего owner.
        Успех: CAS-код возвращён, result JSON-safe, replay TTL равен настройке.
        Нежелательное поведение: stale owner перезаписывает committed cache.
        """
        owner = uuid.uuid4()
        completed = _completed()
        fake = FakeEvalRedis([result])

        saved = await _store(fake, result_ttl_seconds=7200).complete(
            _identity(),
            owner,
            completed,
        )

        arguments = fake.calls[0]
        payload = json.loads(arguments[4])
        assert saved is bool(result)
        assert arguments[3] == str(owner)
        assert arguments[5] == "7200"
        assert payload["request_hash"] == completed.request_hash.hex()
        assert payload["result_payload"]["amount"] == "1250.50"
        assert payload["result_payload"]["created_at"] == "2026-07-28T10:30:00Z"
        assert "owner == ARGV[1]" in arguments[0]

    def test_complete_script_compares_stored_and_result_hashes(self) -> None:
        """
        Проверяем: correct owner не может завершить lease с другим request hash.
        Успех: Lua complete сравнивает HASH.request_hash с ARGV[4].
        Нежелательное поведение: cached result относится к другому payload.
        """
        script = ValkeyHotIdempotencyStore._COMPLETE_SCRIPT

        assert "request_hash" in script
        assert "request_hash == ARGV[4]" in script

    @pytest.mark.parametrize("result", [0, 1])
    async def test_abandon_returns_compare_and_delete_decision(
        self,
        result: int,
    ) -> None:
        """
        Проверяем: abandon удаляет processing entry только по owner-token CAS.
        Успех: Lua 1/0 становится True/False и eval получает текущий owner.
        Нежелательное поведение: rollback одного worker удаляет чужой lease.
        """
        owner = uuid.uuid4()
        fake = FakeEvalRedis([result])

        abandoned = await _store(fake).abandon(_identity(), owner)

        assert abandoned is bool(result)
        assert fake.calls[0][3:] == (str(owner),)
        assert "owner == ARGV[1]" in fake.calls[0][0]
        assert "state == 'processing'" in fake.calls[0][0]

    async def test_begin_passes_owner_request_hash_and_lease_ttl(self) -> None:
        """
        Проверяем: новый processing HASH получает fingerprint, owner и lease TTL.
        Успех: eval получает полный SHA-256 hex, token и 60 секунд одним вызовом.
        Нежелательное поведение: lock создаётся без fingerprint или срока жизни.
        """
        owner = uuid.uuid4()
        request_hash = _request_hash()
        fake = FakeEvalRedis([[1]])

        await _store(fake).begin(
            _identity(),
            request_hash,
            owner,
            lease_seconds=60,
        )

        assert fake.calls[0][3:] == (
            request_hash.hex(),
            str(owner),
            "60",
        )
        assert "redis.call('EXPIRE'" in fake.calls[0][0]


class TestStorageKeyPrivacy:
    async def test_storage_key_contains_only_namespace_and_scoped_digest(
        self,
    ) -> None:
        """
        Проверяем: Redis key не раскрывает PII, operation или raw client key.
        Успех: после namespace находится только 64-символьный SHA-256 digest.
        Нежелательное поведение: keyspace/log Redis раскрывает customer/key data.
        """
        identity = _identity()
        fake = FakeEvalRedis([[1]])

        await _store(fake).begin(
            identity,
            _request_hash(),
            uuid.uuid4(),
            60,
        )

        storage_key = fake.calls[0][2]
        assert re.fullmatch(
            r"test:order-service:idempotency:[0-9a-f]{64}",
            storage_key,
        )
        assert identity.subject_id not in storage_key
        assert identity.operation not in storage_key
        assert "secret-client-key" not in storage_key
        assert identity.key_hash.hex() not in storage_key

    async def test_same_client_key_is_scoped_by_subject_and_operation(
        self,
    ) -> None:
        """
        Проверяем: одинаковый Idempotency-Key из разных scopes не делит HASH.
        Успех: subject и operation изменяют Redis storage digest.
        Нежелательное поведение: один клиент блокирует запрос другого клиента.
        """
        fake = FakeEvalRedis([[1], [1], [1]])
        store = _store(fake)

        for identity in (
            _identity(subject_id="customer-a"),
            _identity(subject_id="customer-b"),
            _identity(subject_id="customer-a", operation="submit_order"),
        ):
            await store.begin(
                identity,
                _request_hash(),
                uuid.uuid4(),
                60,
            )

        keys = {call[2] for call in fake.calls}
        assert len(keys) == 3


class TestValidationAndFailureMapping:
    @pytest.mark.parametrize(
        "factory",
        [
            lambda: _store(FakeEvalRedis([]), result_ttl_seconds=0),
            lambda: _store(FakeEvalRedis([]), namespace=""),
        ],
    )
    def test_rejects_invalid_store_configuration(self, factory: Any) -> None:
        """
        Проверяем: adapter не стартует с вечным/нулевым TTL или пустым namespace.
        Успех: некорректная конфигурация немедленно отклоняется ValueError.
        Нежелательное поведение: production создаёт бессрочные или общие ключи.
        """
        with pytest.raises(ValueError):
            factory()

    @pytest.mark.parametrize(
        ("request_hash", "lease_seconds"),
        [
            (b"short", 60),
            (_request_hash(), 0),
        ],
    )
    async def test_begin_validates_digest_and_lease_before_redis(
        self,
        request_hash: bytes,
        lease_seconds: int,
    ) -> None:
        """
        Проверяем: begin валидирует fingerprint и TTL до внешнего вызова.
        Успех: invalid input даёт ValueError, fake eval не вызывается.
        Нежелательное поведение: мусорный протокол создаёт частичный Redis HASH.
        """
        fake = FakeEvalRedis([])

        with pytest.raises(ValueError):
            await _store(fake).begin(
                _identity(),
                request_hash,
                uuid.uuid4(),
                lease_seconds,
            )

        assert fake.calls == []

    async def test_renew_validates_lease_before_redis(self) -> None:
        """
        Проверяем: heartbeat запрещает нулевой и отрицательный lease.
        Успех: ValueError возникает до eval и состояние Redis не меняется.
        Нежелательное поведение: EXPIRE 0 удаляет активный lock.
        """
        fake = FakeEvalRedis([])

        with pytest.raises(ValueError):
            await _store(fake).renew(_identity(), uuid.uuid4(), 0)

        assert fake.calls == []

    @pytest.mark.parametrize(
        "method_name",
        ["begin", "renew", "complete", "abandon"],
    )
    async def test_maps_only_redis_failures_to_storage_unavailable(
        self,
        method_name: str,
    ) -> None:
        """
        Проверяем: каждый Redis I/O failure имеет единый graceful-fallback error.
        Успех: RedisError маппится в IdempotencyStorageUnavailableError с cause.
        Нежелательное поведение: entrypoint знает redis-py exceptions.
        """
        cause = redis.exceptions.ConnectionError("redis is down")
        store = _store(FakeEvalRedis([cause]))

        with pytest.raises(IdempotencyStorageUnavailableError) as raised:
            if method_name == "begin":
                await store.begin(
                    _identity(),
                    _request_hash(),
                    uuid.uuid4(),
                    60,
                )
            elif method_name == "renew":
                await store.renew(_identity(), uuid.uuid4(), 60)
            elif method_name == "complete":
                await store.complete(
                    _identity(),
                    uuid.uuid4(),
                    _completed(),
                )
            else:
                await store.abandon(_identity(), uuid.uuid4())

        assert raised.value.__cause__ is cause

    async def test_programming_error_is_not_hidden_as_storage_outage(self) -> None:
        """
        Проверяем: graceful fallback ловит только redis-py RedisError.
        Успех: unexpected ValueError пробрасывается без переименования в outage.
        Нежелательное поведение: parser bug незаметно переводит трафик на БД.
        """
        store = _store(FakeEvalRedis([ValueError("broken fake protocol")]))

        with pytest.raises(ValueError, match="broken fake protocol"):
            await store.begin(
                _identity(),
                _request_hash(),
                uuid.uuid4(),
                60,
            )
