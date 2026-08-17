"""
TDD-контракт transport-neutral IdempotencyCoordinator.

Redis хранит горячий lease и replay cache. DurableExecutionPort атомарно выполняет
business callback и записывает завершённый effect в той же PostgreSQL UoW.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.exceptions.idempotency import (
    IdempotencyStorageUnavailableError,
)
from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
    ExecutionResult,
    StoredResult,
)
from app.application.services.idempotency_coordinator import IdempotencyCoordinator
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    BeginAction,
    ExecutionOutcome,
    IdempotencyIdentity,
)

Operation = Callable[[], Awaitable[StoredResult]]


def _identity() -> IdempotencyIdentity:
    return IdempotencyIdentity(
        subject_id=str(uuid.uuid4()),
        operation="create_order",
        key_hash=hash_idempotency_key("client-key"),
    )


def _request_hash(quantity: int = 1) -> bytes:
    return compute_request_hash(
        {"items": [{"productId": "sku-1", "quantity": quantity}]},
        unordered_paths={("items",)},
    )


def _stored() -> StoredResult:
    order_id = uuid.uuid4()
    return StoredResult(
        result_type="generic_snapshot",
        result_payload={
            "id": str(order_id),
            "status": "DRAFT",
            "version": 1,
        },
        result_version=1,
        resource_type="generic_resource",
        resource_id=order_id,
        resource_version=1,
    )


def _completed(request_hash: bytes | None = None) -> CompletedIdempotencyResult:
    stored = _stored()
    return CompletedIdempotencyResult(
        request_hash=request_hash or _request_hash(),
        **stored.model_dump(),
    )


@dataclass
class SequenceOwnerTokenFactory:
    tokens: list[uuid.UUID]
    calls: int = 0

    def __call__(self) -> uuid.UUID:
        token = self.tokens[self.calls]
        self.calls += 1
        return token


@dataclass
class ControlledSleeper:
    requests: asyncio.Queue[tuple[float, asyncio.Future[None]]] = field(
        default_factory=asyncio.Queue
    )

    async def sleep(self, seconds: float) -> None:
        release = asyncio.get_running_loop().create_future()
        await self.requests.put((seconds, release))
        await release

    async def next_request(self) -> tuple[float, asyncio.Future[None]]:
        return await asyncio.wait_for(self.requests.get(), timeout=1)


@dataclass
class FakeHotStore:
    begin_results: list[BeginResult] = field(
        default_factory=lambda: [BeginResult(action=BeginAction.ACQUIRED)]
    )
    begin_error: Exception | None = None
    renew_error: Exception | None = None
    renew_result: bool = True
    complete_result: bool = True
    abandon_result: bool = True
    begin_calls: list[tuple[Any, ...]] = field(default_factory=list)
    renew_calls: list[tuple[Any, ...]] = field(default_factory=list)
    complete_calls: list[tuple[Any, ...]] = field(default_factory=list)
    abandon_calls: list[tuple[Any, ...]] = field(default_factory=list)
    renewed: asyncio.Event = field(default_factory=asyncio.Event)

    async def begin(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        owner_token: uuid.UUID,
        lease_seconds: int,
    ) -> BeginResult:
        self.begin_calls.append((identity, request_hash, owner_token, lease_seconds))
        if self.begin_error is not None:
            raise self.begin_error
        return self.begin_results.pop(0)

    async def renew(
        self,
        identity: IdempotencyIdentity,
        owner_token: uuid.UUID,
        lease_seconds: int,
    ) -> bool:
        self.renew_calls.append((identity, owner_token, lease_seconds))
        self.renewed.set()
        if self.renew_error is not None:
            raise self.renew_error
        return self.renew_result

    async def complete(
        self,
        identity: IdempotencyIdentity,
        owner_token: uuid.UUID,
        result: CompletedIdempotencyResult,
    ) -> bool:
        self.complete_calls.append((identity, owner_token, result))
        return self.complete_result

    async def abandon(
        self,
        identity: IdempotencyIdentity,
        owner_token: uuid.UUID,
    ) -> bool:
        self.abandon_calls.append((identity, owner_token))
        return self.abandon_result


@dataclass
class FakeDurableExecution:
    forced_result: ExecutionResult | None = None
    find_result: ExecutionResult | None = None
    find_calls: list[tuple[IdempotencyIdentity, bytes]] = field(default_factory=list)
    calls: list[tuple[IdempotencyIdentity, bytes]] = field(default_factory=list)
    operation_calls: int = 0

    async def find_existing(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
    ) -> ExecutionResult | None:
        self.find_calls.append((identity, request_hash))
        return self.find_result

    async def execute_once(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        operation: Operation,
    ) -> ExecutionResult:
        self.calls.append((identity, request_hash))
        if self.forced_result is not None:
            return self.forced_result
        self.operation_calls += 1
        stored = await operation()
        completed = CompletedIdempotencyResult(
            request_hash=request_hash,
            **stored.model_dump(),
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.EXECUTED,
            completed=completed,
        )


def _coordinator(
    *,
    hot: FakeHotStore,
    durable: FakeDurableExecution,
    owner_tokens: list[uuid.UUID] | None = None,
    sleeper: ControlledSleeper | None = None,
) -> IdempotencyCoordinator:
    return IdempotencyCoordinator(
        hot_store=hot,
        durable_execution=durable,
        owner_token_factory=SequenceOwnerTokenFactory(owner_tokens or [uuid.uuid4()]),
        sleeper=sleeper or ControlledSleeper(),
    )


class TestOwnerAndDurableFence:
    async def test_nonpositive_lease_is_rejected_before_touching_stores(self) -> None:
        """
        Проверяем: coordinator не создаёт мгновенно истёкший или отрицательный lease.
        Успех: lease_seconds<=0 даёт ValueError до HotStore и durable callback.
        Нежелательное поведение: два workers одновременно считают себя владельцами.
        """
        hot = FakeHotStore()
        durable = FakeDurableExecution()
        coordinator = _coordinator(hot=hot, durable=durable)

        with pytest.raises(ValueError, match="lease"):
            await coordinator.execute(
                _identity(),
                _request_hash(),
                operation=lambda: asyncio.sleep(0, result=_stored()),
                lease_seconds=0,
            )

        assert hot.begin_calls == []
        assert durable.calls == []

    async def test_core_generates_unique_owner_for_each_execution(self) -> None:
        """
        Проверяем: entrypoint не может навязать повторно используемый owner token.
        Успех: два execute получают разные tokens от injected factory.
        Нежелательное поведение: общий lock value позволяет worker завершить чужую работу.
        """
        first_owner = uuid.uuid4()
        second_owner = uuid.uuid4()
        hot = FakeHotStore(
            begin_results=[
                BeginResult(action=BeginAction.ACQUIRED),
                BeginResult(action=BeginAction.ACQUIRED),
            ]
        )
        durable = FakeDurableExecution()
        coordinator = _coordinator(
            hot=hot,
            durable=durable,
            owner_tokens=[first_owner, second_owner],
        )

        await coordinator.execute(
            _identity(),
            _request_hash(),
            operation=lambda: asyncio.sleep(0, result=_stored()),
            lease_seconds=60,
        )
        await coordinator.execute(
            _identity(),
            _request_hash(),
            operation=lambda: asyncio.sleep(0, result=_stored()),
            lease_seconds=60,
        )

        assert [call[2] for call in hot.begin_calls] == [
            first_owner,
            second_owner,
        ]
        assert first_owner != second_owner

    async def test_hot_acquire_always_executes_through_durable_fence(self) -> None:
        """
        Проверяем: Redis lease не доказывает отсутствие committed business effect.
        Успех: ACQUIRED вызывает execute_once, callback и cached CAS completion.
        Нежелательное поведение: потеря Redis позволяет повторить уже созданный заказ.
        """
        hot = FakeHotStore()
        durable = FakeDurableExecution()
        coordinator = _coordinator(hot=hot, durable=durable)
        identity = _identity()
        request_hash = _request_hash()
        stored = _stored()
        completed = CompletedIdempotencyResult(
            request_hash=request_hash,
            **stored.model_dump(),
        )

        result = await coordinator.execute(
            identity,
            request_hash,
            operation=lambda: asyncio.sleep(0, result=stored),
            lease_seconds=60,
        )

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert result.completed == completed
        assert durable.calls == [(identity, request_hash)]
        assert durable.operation_calls == 1
        assert hot.complete_calls[0][2] == completed

    async def test_durable_replay_after_hot_restart_skips_business_callback(
        self,
    ) -> None:
        """
        Проверяем: пустой Redis после рестарта не означает отсутствие effect.
        Успех: ACQUIRED плюс durable REPLAY не вызывает operation второй раз.
        Нежелательное поведение: потерянный HTTP-ответ создаёт второй заказ.
        """
        completed = _completed()
        durable = FakeDurableExecution(
            forced_result=ExecutionResult(
                outcome=ExecutionOutcome.REPLAY,
                completed=completed,
            )
        )
        coordinator = _coordinator(
            hot=FakeHotStore(),
            durable=durable,
        )
        operation_called = False

        async def operation() -> StoredResult:
            nonlocal operation_called
            operation_called = True
            return _stored()

        result = await coordinator.execute(
            _identity(),
            completed.request_hash,
            operation=operation,
            lease_seconds=60,
        )

        assert result.outcome is ExecutionOutcome.REPLAY
        assert result.completed == completed
        assert operation_called is False

    async def test_durable_conflict_skips_business_callback(self) -> None:
        """
        Проверяем: DB record с другим request hash остаётся финальным арбитром.
        Успех: CONFLICT возвращается без вызова operation.
        Нежелательное поведение: Redis restart позволяет переиспользовать старый ключ.
        """
        durable = FakeDurableExecution(
            forced_result=ExecutionResult(
                outcome=ExecutionOutcome.CONFLICT,
            )
        )
        coordinator = _coordinator(
            hot=FakeHotStore(),
            durable=durable,
        )
        operation_called = False

        async def operation() -> StoredResult:
            nonlocal operation_called
            operation_called = True
            return _stored()

        result = await coordinator.execute(
            _identity(),
            _request_hash(2),
            operation=operation,
            lease_seconds=60,
        )

        assert result.outcome is ExecutionOutcome.CONFLICT
        assert operation_called is False


class TestHotDecisions:
    @pytest.mark.parametrize(
        "begin_result, expected_outcome",
        [
            (
                BeginResult(
                    action=BeginAction.REPLAY,
                    completed=_completed(),
                ),
                ExecutionOutcome.REPLAY,
            ),
            (
                BeginResult(action=BeginAction.CONFLICT),
                ExecutionOutcome.CONFLICT,
            ),
            (
                BeginResult(action=BeginAction.IN_PROGRESS),
                ExecutionOutcome.IN_PROGRESS,
            ),
        ],
    )
    async def test_terminal_hot_decision_skips_durable_execution(
        self,
        begin_result: BeginResult,
        expected_outcome: ExecutionOutcome,
    ) -> None:
        """
        Проверяем: готовое решение HotStore не запускает второй coordination path.
        Успех: REPLAY, CONFLICT и IN_PROGRESS возвращаются без execute_once.
        Нежелательное поведение: параллельный request обходит действующий lease.
        """
        hot = FakeHotStore(begin_results=[begin_result])
        durable = FakeDurableExecution()
        coordinator = _coordinator(hot=hot, durable=durable)

        result = await coordinator.execute(
            _identity(),
            _request_hash(),
            operation=lambda: asyncio.sleep(0, result=_stored()),
            lease_seconds=60,
        )

        assert result.outcome is expected_outcome
        assert result.completed == begin_result.completed
        assert durable.calls == []


class TestPreparationBeforeDurableMutation:
    @pytest.mark.parametrize("hot_mode", ["absent", "unavailable"])
    async def test_durable_replay_skips_preparation_on_slow_paths(
        self,
        hot_mode: str,
    ) -> None:
        """
        Проверяем: no-hot и hot-outage retry сначала читают durable result.
        Успех: REPLAY возвращается без external preparation и execute_once.
        Нежелательное поведение: committed retry зависит от повторной доступности Pricing.
        """
        identity = _identity()
        request_hash = _request_hash()
        completed = _completed(request_hash)
        durable = FakeDurableExecution(
            find_result=ExecutionResult(
                outcome=ExecutionOutcome.REPLAY,
                completed=completed,
            )
        )
        hot = (
            None
            if hot_mode == "absent"
            else FakeHotStore(begin_error=IdempotencyStorageUnavailableError())
        )
        coordinator = IdempotencyCoordinator(
            hot_store=hot,
            durable_execution=durable,
        )
        prepare_calls = 0

        async def prepare() -> None:
            nonlocal prepare_calls
            prepare_calls += 1

        result = await coordinator.execute(
            identity,
            request_hash,
            operation=lambda: asyncio.sleep(0, result=_stored()),
            lease_seconds=60,
            prepare=prepare,
        )

        assert result.outcome is ExecutionOutcome.REPLAY
        assert result.completed == completed
        assert prepare_calls == 0
        assert durable.find_calls == [(identity, request_hash)]
        assert durable.calls == []

    async def test_hot_acquire_still_checks_durable_before_preparation(
        self,
    ) -> None:
        """
        Проверяем: Redis ACQUIRED не доказывает отсутствие committed DB effect.
        Успех: durable REPLAY пропускает prepare/callback и заполняет hot result.
        Нежелательное поведение: Redis restart повторно вызывает внешний Pricing.
        """
        identity = _identity()
        request_hash = _request_hash()
        completed = _completed(request_hash)
        durable = FakeDurableExecution(
            find_result=ExecutionResult(
                outcome=ExecutionOutcome.REPLAY,
                completed=completed,
            )
        )
        hot = FakeHotStore()
        coordinator = _coordinator(hot=hot, durable=durable)
        prepare_calls = 0

        async def prepare() -> None:
            nonlocal prepare_calls
            prepare_calls += 1

        result = await coordinator.execute(
            identity,
            request_hash,
            operation=lambda: asyncio.sleep(0, result=_stored()),
            lease_seconds=60,
            prepare=prepare,
        )

        assert result.outcome is ExecutionOutcome.REPLAY
        assert prepare_calls == 0
        assert durable.calls == []
        assert hot.complete_calls[0][2] == completed

    async def test_preparation_finishes_before_durable_callback(self) -> None:
        """
        Проверяем: внешний preparation не выполняется внутри durable UoW callback.
        Успех: prepare завершается до execute_once и operation видит готовые данные.
        Нежелательное поведение: Pricing удерживает DB transaction и row locks.
        """
        durable = FakeDurableExecution()
        coordinator = IdempotencyCoordinator(
            hot_store=None,
            durable_execution=durable,
        )
        prepared = False

        async def prepare() -> None:
            nonlocal prepared
            prepared = True

        async def operation() -> StoredResult:
            assert prepared is True
            return _stored()

        result = await coordinator.execute(
            _identity(),
            _request_hash(),
            operation=operation,
            lease_seconds=60,
            prepare=prepare,
        )

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert len(durable.find_calls) == 1
        assert durable.operation_calls == 1


class TestHeartbeatAndCAS:
    async def test_long_operation_renews_before_half_of_lease(self) -> None:
        """
        Проверяем: живой worker продлевает lease до окончания операции дольше 60 секунд.
        Успех: heartbeat interval меньше 30 секунд и renew использует тот же owner.
        Нежелательное поведение: второй worker получает lock во время активной работы.
        """
        owner_token = uuid.uuid4()
        hot = FakeHotStore()
        durable = FakeDurableExecution()
        sleeper = ControlledSleeper()
        coordinator = _coordinator(
            hot=hot,
            durable=durable,
            owner_tokens=[owner_token],
            sleeper=sleeper,
        )
        operation_started = asyncio.Event()
        finish_operation = asyncio.Event()

        async def operation() -> StoredResult:
            operation_started.set()
            await finish_operation.wait()
            return _stored()

        execution = asyncio.create_task(
            coordinator.execute(
                _identity(),
                _request_hash(),
                operation=operation,
                lease_seconds=60,
            )
        )
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        interval, release_tick = await sleeper.next_request()
        release_tick.set_result(None)
        await asyncio.wait_for(hot.renewed.wait(), timeout=1)
        finish_operation.set()
        await asyncio.wait_for(execution, timeout=1)

        assert 0 < interval < 30
        assert hot.renew_calls[0][1] == owner_token
        assert hot.renew_calls[0][2] == 60

    async def test_lost_lease_forbids_hot_completion_but_keeps_durable_result(
        self,
    ) -> None:
        """
        Проверяем: stale worker не пишет cache поверх нового владельца.
        Успех: renew=False запрещает hot complete, durable EXECUTED остаётся результатом.
        Нежелательное поведение: старый owner затирает cached replay нового worker.
        """
        hot = FakeHotStore(renew_result=False)
        durable = FakeDurableExecution()
        sleeper = ControlledSleeper()
        coordinator = _coordinator(
            hot=hot,
            durable=durable,
            sleeper=sleeper,
        )
        operation_started = asyncio.Event()
        finish_operation = asyncio.Event()

        async def operation() -> StoredResult:
            operation_started.set()
            await finish_operation.wait()
            return _stored()

        execution = asyncio.create_task(
            coordinator.execute(
                _identity(),
                _request_hash(),
                operation=operation,
                lease_seconds=60,
            )
        )
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        _, release_tick = await sleeper.next_request()
        release_tick.set_result(None)
        await asyncio.wait_for(hot.renewed.wait(), timeout=1)
        finish_operation.set()
        result = await asyncio.wait_for(execution, timeout=1)

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert hot.complete_calls == []

    async def test_heartbeat_storage_outage_is_treated_as_lost_lease(self) -> None:
        """
        Проверяем: Redis падает после acquire, пока PostgreSQL mutation ещё выполняется.
        Успех: durable result возвращается, hot complete не вызывается, outage не маскирует commit.
        Нежелательное поведение: клиент получает failure после уже совершённого эффекта.
        """
        hot = FakeHotStore(renew_error=IdempotencyStorageUnavailableError())
        durable = FakeDurableExecution()
        sleeper = ControlledSleeper()
        coordinator = _coordinator(
            hot=hot,
            durable=durable,
            sleeper=sleeper,
        )
        operation_started = asyncio.Event()
        finish_operation = asyncio.Event()

        async def operation() -> StoredResult:
            operation_started.set()
            await finish_operation.wait()
            return _stored()

        execution = asyncio.create_task(
            coordinator.execute(
                _identity(),
                _request_hash(),
                operation=operation,
                lease_seconds=60,
            )
        )
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        _, release_tick = await sleeper.next_request()
        release_tick.set_result(None)
        await asyncio.wait_for(hot.renewed.wait(), timeout=1)
        finish_operation.set()
        result = await asyncio.wait_for(execution, timeout=1)

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert hot.complete_calls == []

    async def test_unexpected_renew_error_does_not_hide_durable_success(
        self,
    ) -> None:
        """
        Проверяем: heartbeat bug не меняет уже определённый durable outcome.
        Успех: EXECUTED возвращается без hot complete после renew RuntimeError.
        Нежелательное поведение: клиент видит 500 после committed business effect.
        """
        hot = FakeHotStore(renew_error=RuntimeError("broken renew parser"))
        sleeper = ControlledSleeper()
        coordinator = _coordinator(
            hot=hot,
            durable=FakeDurableExecution(),
            sleeper=sleeper,
        )
        operation_started = asyncio.Event()
        finish_operation = asyncio.Event()

        async def operation() -> StoredResult:
            operation_started.set()
            await finish_operation.wait()
            return _stored()

        execution = asyncio.create_task(
            coordinator.execute(
                _identity(),
                _request_hash(),
                operation=operation,
                lease_seconds=60,
            )
        )
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        _, release_tick = await sleeper.next_request()
        release_tick.set_result(None)
        await asyncio.wait_for(hot.renewed.wait(), timeout=1)
        finish_operation.set()

        result = await asyncio.wait_for(execution, timeout=1)

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert hot.complete_calls == []

    async def test_unexpected_renew_error_does_not_mask_business_error(
        self,
    ) -> None:
        """
        Проверяем: heartbeat failure не заменяет исходную ошибку operation.
        Успех: caller получает business ValueError, lease считается потерянным.
        Нежелательное поведение: renew RuntimeError скрывает причину rollback.
        """
        hot = FakeHotStore(renew_error=RuntimeError("broken renew parser"))
        sleeper = ControlledSleeper()
        coordinator = _coordinator(
            hot=hot,
            durable=FakeDurableExecution(),
            sleeper=sleeper,
        )
        operation_started = asyncio.Event()
        finish_operation = asyncio.Event()

        async def operation() -> StoredResult:
            operation_started.set()
            await finish_operation.wait()
            raise ValueError("business rollback")

        execution = asyncio.create_task(
            coordinator.execute(
                _identity(),
                _request_hash(),
                operation=operation,
                lease_seconds=60,
            )
        )
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        _, release_tick = await sleeper.next_request()
        release_tick.set_result(None)
        await asyncio.wait_for(hot.renewed.wait(), timeout=1)
        finish_operation.set()

        with pytest.raises(ValueError, match="business rollback"):
            await asyncio.wait_for(execution, timeout=1)

        assert hot.complete_calls == []
        assert hot.abandon_calls == []

    async def test_operation_error_abandons_only_current_owner(self) -> None:
        """
        Проверяем: rollback path снимает lease через compare-and-delete.
        Успех: исходная ошибка пробрасывается, abandon получает owner этого execute.
        Нежелательное поведение: ошибка оставляет lock до TTL или удаляет чужой lease.
        """
        owner_token = uuid.uuid4()
        hot = FakeHotStore()
        coordinator = _coordinator(
            hot=hot,
            durable=FakeDurableExecution(),
            owner_tokens=[owner_token],
        )
        identity = _identity()

        async def operation() -> StoredResult:
            raise ValueError("business rollback")

        with pytest.raises(ValueError, match="business rollback"):
            await coordinator.execute(
                identity,
                _request_hash(),
                operation=operation,
                lease_seconds=60,
            )

        assert hot.abandon_calls == [(identity, owner_token)]


class TestRedisFailureFallback:
    async def test_known_hot_outage_executes_directly_through_durable_port(
        self,
    ) -> None:
        """
        Проверяем: Redis outage не делает optional dependency обязательной.
        Успех: StorageUnavailable ведёт в execute_once и не отвечает клиенту 503.
        Нежелательное поведение: заказ нельзя создать при здоровом PostgreSQL.
        """
        hot = FakeHotStore(begin_error=IdempotencyStorageUnavailableError())
        durable = FakeDurableExecution()
        coordinator = _coordinator(hot=hot, durable=durable)

        result = await coordinator.execute(
            _identity(),
            _request_hash(),
            operation=lambda: asyncio.sleep(0, result=_stored()),
            lease_seconds=60,
        )

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert durable.operation_calls == 1
        assert hot.complete_calls == []

    async def test_unexpected_hot_error_is_not_hidden(self) -> None:
        """
        Проверяем: graceful degradation ловит только известную недоступность storage.
        Успех: programming error пробрасывается и durable operation не запускается.
        Нежелательное поведение: повреждение протокола незаметно уходит в DB slow path.
        """
        durable = FakeDurableExecution()
        coordinator = _coordinator(
            hot=FakeHotStore(begin_error=RuntimeError("broken parser")),
            durable=durable,
        )

        with pytest.raises(RuntimeError, match="broken parser"):
            await coordinator.execute(
                _identity(),
                _request_hash(),
                operation=lambda: asyncio.sleep(0, result=_stored()),
                lease_seconds=60,
            )

        assert durable.calls == []
