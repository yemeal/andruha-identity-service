"""
Тесты тонкого CircuitBreakingHotStore adapter.

State machine живёт в переиспользованном CircuitBreaker. Adapter только проводит
полный HotStore protocol через call() и переводит open circuit в storage outage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.exceptions.idempotency import (
    IdempotencyStorageUnavailableError,
)
from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
    StoredResult,
)
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    BeginAction,
    IdempotencyIdentity,
)
from app.infrastructure.resilience.circuit_breaker import CircuitBreakerError
from app.infrastructure.resilience.circuit_breaking_hot_store import (
    CircuitBreakingHotStore,
)


@dataclass
class RecordingCircuitBreaker:
    error: Exception | None = None
    calls: list[tuple[Any, tuple[Any, ...]]] = field(default_factory=list)

    async def call(self, func, *args):
        self.calls.append((func, args))
        if self.error is not None:
            raise self.error
        return await func(*args)


@dataclass
class FakeHotStore:
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    error: Exception | None = None

    async def begin(self, *args) -> BeginResult:
        return self._result(
            "begin",
            args,
            BeginResult(action=BeginAction.ACQUIRED),
        )

    async def renew(self, *args) -> bool:
        return self._result("renew", args, True)

    async def complete(self, *args) -> bool:
        return self._result("complete", args, True)

    async def abandon(self, *args) -> bool:
        return self._result("abandon", args, True)

    def _result(self, method: str, args: tuple[Any, ...], value):
        self.calls.append((method, args))
        if self.error is not None:
            raise self.error
        return value


def _identity() -> IdempotencyIdentity:
    return IdempotencyIdentity(
        subject_id=str(uuid.uuid4()),
        operation="submit_order",
        key_hash=hash_idempotency_key("key"),
    )


def _completed() -> CompletedIdempotencyResult:
    resource_id = uuid.uuid4()
    stored = StoredResult(
        result_type="generic_snapshot",
        result_payload={"id": str(resource_id)},
        result_version=1,
        resource_type="generic_resource",
        resource_id=resource_id,
        resource_version=1,
    )
    return CompletedIdempotencyResult(
        request_hash=compute_request_hash({"version": 1}),
        **stored.model_dump(),
    )


class TestFullHotStoreProtocol:
    @pytest.mark.parametrize(
        "method,args",
        [
            (
                "begin",
                (
                    _identity(),
                    compute_request_hash({"version": 1}),
                    uuid.uuid4(),
                    60,
                ),
            ),
            ("renew", (_identity(), uuid.uuid4(), 60)),
            ("complete", (_identity(), uuid.uuid4(), _completed())),
            ("abandon", (_identity(), uuid.uuid4())),
        ],
    )
    async def test_each_method_runs_through_shared_circuit_breaker(
        self,
        method: str,
        args: tuple[Any, ...],
    ) -> None:
        """
        Проверяем: adapter покрывает acquire, heartbeat, completion и cleanup.
        Успех: каждый вызов проходит ровно через один cb.call и внутренний HotStore.
        Нежелательное поведение: часть протокола обходит fail-fast или даёт AttributeError.
        """
        inner = FakeHotStore()
        circuit_breaker = RecordingCircuitBreaker()
        store = CircuitBreakingHotStore(
            inner=inner,
            circuit_breaker=circuit_breaker,
        )

        await getattr(store, method)(*args)

        assert inner.calls == [(method, args)]
        assert circuit_breaker.calls == [(getattr(inner, method), args)]


class TestErrorMapping:
    async def test_open_circuit_becomes_known_storage_outage(self) -> None:
        """
        Проверяем: core не зависит от infrastructure CircuitBreakerError.
        Успех: open circuit маппится в IdempotencyStorageUnavailableError.
        Нежелательное поведение: coordinator не включает durable fallback.
        """
        inner = FakeHotStore()
        store = CircuitBreakingHotStore(
            inner=inner,
            circuit_breaker=RecordingCircuitBreaker(error=CircuitBreakerError()),
        )

        with pytest.raises(IdempotencyStorageUnavailableError):
            await store.begin(
                _identity(),
                compute_request_hash({"version": 1}),
                uuid.uuid4(),
                60,
            )

        assert inner.calls == []

    async def test_inner_storage_outage_is_not_retyped_as_programming_error(
        self,
    ) -> None:
        """
        Проверяем: Redis adapter outage проходит через breaker failure filter.
        Успех: наружу выходит тот же application storage-unavailable тип.
        Нежелательное поведение: известный outage маскируется неожиданной ошибкой.
        """
        inner = FakeHotStore(error=IdempotencyStorageUnavailableError())
        store = CircuitBreakingHotStore(
            inner=inner,
            circuit_breaker=RecordingCircuitBreaker(),
        )

        with pytest.raises(IdempotencyStorageUnavailableError):
            await store.renew(_identity(), uuid.uuid4(), 60)

    async def test_unexpected_inner_error_propagates(self) -> None:
        """
        Проверяем: adapter не скрывает contract и programming failures.
        Успех: RuntimeError пробрасывается без маппинга в graceful degradation.
        Нежелательное поведение: bug незаметно отправляет весь трафик в DB slow path.
        """
        inner = FakeHotStore(error=RuntimeError("broken parser"))
        store = CircuitBreakingHotStore(
            inner=inner,
            circuit_breaker=RecordingCircuitBreaker(),
        )

        with pytest.raises(RuntimeError, match="broken parser"):
            await store.abandon(_identity(), uuid.uuid4())
