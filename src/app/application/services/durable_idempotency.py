from __future__ import annotations

from app.application.ports.dto.idempotency import (
    CompletedIdempotencyResult,
    ExecutionResult,
)
from app.application.ports.idempotency import (
    IdempotencyRecordRepositoryProtocol,
    IdempotentOperation,
)
from app.application.ports.uow import AsyncUOWProtocol
from app.application.value_objects.idempotency import (
    ExecutionOutcome,
    IdempotencyIdentity,
)


class _ConcurrentWinnerCommitted(Exception):
    pass


class DurableExecutionService:
    """
    Atomically binds a completed record to the business callback.

    The callback may only stage rollback-safe writes through the same UoW.
    Network I/O and external effects are forbidden inside it.
    """

    def __init__(
        self,
        records: IdempotencyRecordRepositoryProtocol,
        uow: AsyncUOWProtocol,
    ) -> None:
        self._records = records
        self._uow = uow

    async def execute_once(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        operation: IdempotentOperation,
    ) -> ExecutionResult:
        _validate_digest(request_hash)
        existing = await self.find_existing(identity, request_hash)
        if existing is not None:
            return existing

        try:
            async with self._uow:
                stored = await operation()
                completed = CompletedIdempotencyResult(
                    request_hash=request_hash,
                    **stored.model_dump(),
                )
                if not await self._records.try_add_completed(identity, completed):
                    # Roll back every staged business write after losing the fence.
                    raise _ConcurrentWinnerCommitted
        except _ConcurrentWinnerCommitted as race_error:
            winner = await self.find_existing(identity, request_hash)
            if winner is None:
                raise RuntimeError(
                    "idempotency unique conflict without a committed winner"
                ) from race_error
            return winner
        except Exception as operation_error:
            # Concurrent durable requests can pass the initial read together.
            # After rollback, re-read the winner instead of leaking a stale-state error.
            try:
                winner = await self.find_existing(identity, request_hash)
            except Exception as lookup_error:
                raise operation_error from lookup_error
            if winner is not None:
                return winner
            raise

        return ExecutionResult(
            outcome=ExecutionOutcome.EXECUTED,
            completed=completed,
        )

    async def find_existing(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
    ) -> ExecutionResult | None:
        """Классифицирует durable result до внешней preparation-фазы."""
        _validate_digest(request_hash)
        # Даже SELECT начинает транзакцию в SQLAlchemy. Короткая read-UoW
        # гарантированно завершается до потенциально медленного prepare I/O.
        async with self._uow:
            existing = await self._records.get_completed(identity)
        if existing is None:
            return None
        return _classify(existing, request_hash)


def _validate_digest(request_hash: bytes) -> None:
    if len(request_hash) != 32:
        raise ValueError("request_hash must be a full SHA-256 digest")


def _classify(
    completed: CompletedIdempotencyResult,
    request_hash: bytes,
) -> ExecutionResult:
    if completed.request_hash != request_hash:
        return ExecutionResult(outcome=ExecutionOutcome.CONFLICT)
    return ExecutionResult(
        outcome=ExecutionOutcome.REPLAY,
        completed=completed,
    )
