"""
TDD-контракт PostgreSQL durable fence.

Business effect и idempotency record становятся видимыми только одним commit.
При unique race проигравшая транзакция откатывает свой callback целиком.
Operation contract допускает только локальные DB writes через ту же UoW:
внешний I/O внутри callback неоткатываем и поэтому запрещён.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.ports.dto.idempotency import (
    CompletedIdempotencyResult,
    StoredResult,
)
from app.application.services.durable_idempotency import DurableExecutionService
from app.application.services.idempotency_coordinator import IdempotencyCoordinator
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    ExecutionOutcome,
    IdempotencyIdentity,
)


class ConcurrentStateChangedError(Exception):
    pass


def _identity() -> IdempotencyIdentity:
    return IdempotencyIdentity(
        subject_id=str(uuid.uuid4()),
        operation="submit_order",
        key_hash=hash_idempotency_key("submit-key"),
    )


def _request_hash(version: int = 1) -> bytes:
    return compute_request_hash({"orderVersion": version})


def _stored(status: str = "PENDING") -> StoredResult:
    order_id = uuid.uuid4()
    return StoredResult(
        result_type="generic_snapshot",
        result_payload={
            "id": str(order_id),
            "status": status,
            "version": 2,
        },
        result_version=1,
        resource_type="generic_resource",
        resource_id=order_id,
        resource_version=2,
    )


def _completed(
    request_hash: bytes,
    stored: StoredResult | None = None,
) -> CompletedIdempotencyResult:
    result = stored or _stored()
    return CompletedIdempotencyResult(
        request_hash=request_hash,
        **result.model_dump(),
    )


@dataclass
class FakeTransactionalSession:
    effects: list[dict[str, Any]] = field(default_factory=list)
    records: dict[IdempotencyIdentity, CompletedIdempotencyResult] = field(
        default_factory=dict
    )
    pending_effects: list[dict[str, Any]] = field(default_factory=list)
    pending_records: dict[
        IdempotencyIdentity,
        CompletedIdempotencyResult,
    ] = field(default_factory=dict)
    commits: int = 0
    rollbacks: int = 0
    in_transaction: bool = False
    commit_snapshots: list[tuple[int, int]] = field(default_factory=list)

    def stage_effect(self, effect: dict[str, Any]) -> None:
        self.pending_effects.append(effect)

    def commit(self) -> None:
        self.effects.extend(self.pending_effects)
        self.records.update(self.pending_records)
        self._clear_pending()
        self.commits += 1
        self.commit_snapshots.append((len(self.effects), len(self.records)))

    def rollback(self) -> None:
        self._clear_pending()
        self.rollbacks += 1

    def _clear_pending(self) -> None:
        self.pending_effects = []
        self.pending_records = {}


class FakeUOW:
    def __init__(self, session: FakeTransactionalSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeUOW:
        self._session.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.in_transaction = False


class FakeIdempotencyRecordRepository:
    def __init__(self, session: FakeTransactionalSession) -> None:
        self._session = session
        self.conflict_on_add: CompletedIdempotencyResult | None = None

    async def get_completed(
        self,
        identity: IdempotencyIdentity,
    ) -> CompletedIdempotencyResult | None:
        # SQLAlchemy SELECT делает autobegin даже без явного UoW context.
        self._session.in_transaction = True
        return self._session.pending_records.get(identity) or self._session.records.get(
            identity
        )

    async def try_add_completed(
        self,
        identity: IdempotencyIdentity,
        completed: CompletedIdempotencyResult,
    ) -> bool:
        if self.conflict_on_add is not None:
            self._session.records[identity] = self.conflict_on_add
            self.conflict_on_add = None
            return False
        if identity in self._session.records:
            return False
        self._session.pending_records[identity] = completed
        return True


def _service(
    session: FakeTransactionalSession,
    records: FakeIdempotencyRecordRepository | None = None,
) -> tuple[DurableExecutionService, FakeIdempotencyRecordRepository]:
    repository = records or FakeIdempotencyRecordRepository(session)
    return (
        DurableExecutionService(
            records=repository,
            uow=FakeUOW(session),
        ),
        repository,
    )


class TestAtomicExecution:
    async def test_effect_and_completed_record_share_one_commit(self) -> None:
        """
        Проверяем: business mutation и durable replay record атомарны.
        Успех: один commit одновременно делает видимыми effect и record.
        Нежелательное поведение: dual write оставляет заказ без idempotency fence.
        """
        session = FakeTransactionalSession()
        service, _ = _service(session)
        identity = _identity()
        request_hash = _request_hash()
        stored = _stored()

        async def operation() -> StoredResult:
            assert session.in_transaction is True
            session.stage_effect({"order_id": stored.resource_id})
            return stored

        result = await service.execute_once(
            identity,
            request_hash,
            operation,
        )

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert result.completed == _completed(request_hash, stored)
        assert session.commit_snapshots == [(0, 0), (1, 1)]
        assert session.commit_snapshots.count((1, 1)) == 1
        assert session.rollbacks == 0

    async def test_operation_failure_rolls_back_effect_and_record(self) -> None:
        """
        Проверяем: callback exception не оставляет половину идемпотентной mutation.
        Успех: исходная ошибка проброшена, staged effect и record отсутствуют.
        Нежелательное поведение: retry видит replay для незавершённой операции.
        """
        session = FakeTransactionalSession()
        service, _ = _service(session)

        async def operation() -> StoredResult:
            session.stage_effect({"order_id": uuid.uuid4()})
            raise RuntimeError("business failure")

        with pytest.raises(RuntimeError, match="business failure"):
            await service.execute_once(
                _identity(),
                _request_hash(),
                operation,
            )

        assert session.effects == []
        assert session.records == {}
        assert session.commit_snapshots == [(0, 0), (0, 0)]
        assert session.rollbacks == 1


class TestPreparationTransactionBoundary:
    async def test_find_existing_closes_read_transaction(self) -> None:
        """
        Проверяем: durable preflight SELECT не оставляет autobegin transaction.
        Успех: после find_existing session свободна до внешнего preparation.
        Нежелательное поведение: Pricing выполняется при открытой DB transaction.
        """
        session = FakeTransactionalSession()
        service, _ = _service(session)

        result = await service.find_existing(
            _identity(),
            _request_hash(),
        )

        assert result is None
        assert session.in_transaction is False
        assert session.commits == 1

    async def test_coordinator_prepares_only_after_read_transaction_closes(
        self,
    ) -> None:
        """
        Проверяем: replay preflight и external preparation разделены границей UoW.
        Успех: prepare вне transaction, callback внутри новой durable transaction.
        Нежелательное поведение: медленный Catalog удерживает соединение или row snapshot.
        """
        session = FakeTransactionalSession()
        durable, _ = _service(session)
        coordinator = IdempotencyCoordinator(
            hot_store=None,
            durable_execution=durable,
        )
        prepared = False

        async def prepare() -> None:
            nonlocal prepared
            assert session.in_transaction is False
            prepared = True

        async def operation() -> StoredResult:
            assert prepared is True
            assert session.in_transaction is True
            stored = _stored()
            session.stage_effect({"order_id": stored.resource_id})
            return stored

        result = await coordinator.execute(
            _identity(),
            _request_hash(),
            operation=operation,
            lease_seconds=60,
            prepare=prepare,
        )

        assert result.outcome is ExecutionOutcome.EXECUTED
        assert len(session.effects) == 1
        assert session.in_transaction is False


class TestReplayAndConflict:
    async def test_existing_same_request_replays_without_callback(self) -> None:
        """
        Проверяем: повтор после потерянного ответа не повторяет business effect.
        Успех: тот же request hash возвращает REPLAY без вызова callback.
        Нежелательное поведение: клиентский retry повторно submit-ит заказ.
        """
        session = FakeTransactionalSession()
        identity = _identity()
        request_hash = _request_hash()
        completed = _completed(request_hash)
        session.records[identity] = completed
        service, _ = _service(session)
        operation_called = False

        async def operation() -> StoredResult:
            nonlocal operation_called
            operation_called = True
            return _stored()

        result = await service.execute_once(
            identity,
            request_hash,
            operation,
        )

        assert result.outcome is ExecutionOutcome.REPLAY
        assert result.completed == completed
        assert operation_called is False

    async def test_existing_different_request_is_conflict(self) -> None:
        """
        Проверяем: тот же scoped key нельзя переиспользовать с другим payload.
        Успех: другой request hash возвращает CONFLICT без callback.
        Нежелательное поведение: под старым ключом выполняется новая команда.
        """
        session = FakeTransactionalSession()
        identity = _identity()
        session.records[identity] = _completed(_request_hash(1))
        service, _ = _service(session)
        operation_called = False

        async def operation() -> StoredResult:
            nonlocal operation_called
            operation_called = True
            return _stored()

        result = await service.execute_once(
            identity,
            _request_hash(2),
            operation,
        )

        assert result.outcome is ExecutionOutcome.CONFLICT
        assert result.completed is None
        assert operation_called is False

    async def test_unique_race_rolls_back_loser_then_replays_winner(self) -> None:
        """
        Проверяем: два concurrent requests не коммитят два business effects.
        Успех: loser откатывает callback, перечитывает winner и возвращает REPLAY.
        Нежелательное поведение: ON CONFLICT скрывает уже выполненный второй effect.
        """
        session = FakeTransactionalSession()
        identity = _identity()
        request_hash = _request_hash()
        winner = _completed(request_hash)
        records = FakeIdempotencyRecordRepository(session)
        records.conflict_on_add = winner
        service, _ = _service(session, records)

        async def operation() -> StoredResult:
            session.stage_effect({"order_id": uuid.uuid4()})
            return _stored()

        result = await service.execute_once(
            identity,
            request_hash,
            operation,
        )

        assert result.outcome is ExecutionOutcome.REPLAY
        assert result.completed == winner
        assert session.effects == []
        assert session.rollbacks == 1
        assert session.records[identity] == winner

    async def test_unique_race_with_other_payload_returns_conflict(self) -> None:
        """
        Проверяем: concurrent reuse ключа с другим payload не переигрывает winner.
        Успех: loser rollback-ится и после reread получает CONFLICT.
        Нежелательное поведение: разные команды считаются одним безопасным retry.
        """
        session = FakeTransactionalSession()
        identity = _identity()
        records = FakeIdempotencyRecordRepository(session)
        records.conflict_on_add = _completed(_request_hash(1))
        service, _ = _service(session, records)

        async def operation() -> StoredResult:
            session.stage_effect({"order_id": uuid.uuid4()})
            return _stored()

        result = await service.execute_once(
            identity,
            _request_hash(2),
            operation,
        )

        assert result.outcome is ExecutionOutcome.CONFLICT
        assert result.completed is None
        assert session.effects == []
        assert session.rollbacks == 1

    async def test_operation_lock_race_rereads_committed_winner(
        self,
    ) -> None:
        """
        Проверяем: Redis-down race может проиграться до INSERT idempotency record.
        Успех: stale-version callback rollback-ится и committed winner даёт REPLAY.
        Нежелательное поведение: same-key concurrent retry случайно получает HTTP 412.
        """
        session = FakeTransactionalSession()
        identity = _identity()
        request_hash = _request_hash()
        winner = _completed(request_hash)
        service, _ = _service(session)

        async def operation() -> StoredResult:
            session.stage_effect({"status": "duplicate pending mutation"})
            # Имитирует commit первого запроса, пока второй ждал aggregate row lock.
            session.records[identity] = winner
            raise ConcurrentStateChangedError("aggregate advanced by winner")

        result = await service.execute_once(
            identity,
            request_hash,
            operation,
        )

        assert result.outcome is ExecutionOutcome.REPLAY
        assert result.completed == winner
        assert session.effects == []
        assert session.rollbacks == 1
