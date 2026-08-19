from __future__ import annotations

import asyncio
from contextlib import suppress
import uuid

from app.application.exceptions.idempotency import (
    IdempotencyStorageUnavailableError,
)
from app.application.ports.dto.idempotency import (
    ExecutionResult,
)
from app.application.ports.idempotency import (
    AsyncSleeper,
    DurableExecutionProtocol,
    HotIdempotencyStoreProtocol,
    IdempotencyObserverProtocol,
    IdempotencyPreparation,
    IdempotentOperation,
    OwnerTokenFactory,
)
from app.application.value_objects.idempotency import (
    BeginAction,
    ExecutionOutcome,
    IdempotencyIdentity,
)


class _AsyncioSleeper:
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _NoOpIdempotencyObserver:
    def observe_outcome(self, outcome: str) -> None:
        pass

    def observe_hot_degraded(self, stage: str) -> None:
        pass


class IdempotencyCoordinator:
    """
    Coordinates an optional hot lease and the mandatory durable fence.

    Optional preparation runs after replay lookup and outside the DB transaction.
    It must be read-only and retry-safe because it is not a business effect.
    """

    def __init__(
        self,
        hot_store: HotIdempotencyStoreProtocol | None,
        durable_execution: DurableExecutionProtocol,
        owner_token_factory: OwnerTokenFactory = uuid.uuid7,
        sleeper: AsyncSleeper | None = None,
        observer: IdempotencyObserverProtocol | None = None,
    ) -> None:
        self._hot_store = hot_store
        self._durable_execution = durable_execution
        self._owner_token_factory = owner_token_factory
        self._sleeper = sleeper or _AsyncioSleeper()
        self._observer = observer or _NoOpIdempotencyObserver()

    async def execute(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        operation: IdempotentOperation,
        *,
        lease_seconds: int,
        prepare: IdempotencyPreparation | None = None,
    ) -> ExecutionResult:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if len(request_hash) != 32:
            raise ValueError("request_hash must be a full SHA-256 digest")
        if self._hot_store is None:
            self._observer.observe_hot_degraded("disabled")
            return self._observe(
                await self._execute_durable(
                    identity,
                    request_hash,
                    operation,
                    prepare=prepare,
                )
            )

        owner_token = self._owner_token_factory()
        try:
            begin = await self._hot_store.begin(
                identity,
                request_hash,
                owner_token,
                lease_seconds,
            )
        except IdempotencyStorageUnavailableError:
            self._observer.observe_hot_degraded("begin")
            return self._observe(
                await self._execute_durable(
                    identity,
                    request_hash,
                    operation,
                    prepare=prepare,
                )
            )

        if begin.action is BeginAction.REPLAY:
            return self._observe(
                ExecutionResult(
                    outcome=ExecutionOutcome.REPLAY,
                    completed=begin.completed,
                )
            )
        if begin.action is BeginAction.CONFLICT:
            return self._observe(ExecutionResult(outcome=ExecutionOutcome.CONFLICT))
        if begin.action is BeginAction.IN_PROGRESS:
            durable_result = await self._durable_execution.find_existing(
                identity,
                request_hash,
            )
            if durable_result is not None:
                return self._observe(durable_result)
            return self._observe(ExecutionResult(outcome=ExecutionOutcome.IN_PROGRESS))

        lost_lease = asyncio.Event()
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                identity=identity,
                owner_token=owner_token,
                lease_seconds=lease_seconds,
                lost_lease=lost_lease,
                stop=stop_heartbeat,
            )
        )
        try:
            existing = (
                await self._durable_execution.find_existing(
                    identity,
                    request_hash,
                )
                if prepare is not None
                else None
            )
            if existing is not None:
                result = existing
            else:
                if prepare is not None:
                    await prepare()
                result = await self._durable_execution.execute_once(
                    identity,
                    request_hash,
                    operation,
                )
        except BaseException:
            await self._stop_heartbeat(heartbeat, stop_heartbeat)
            if not lost_lease.is_set():
                try:
                    await self._hot_store.abandon(identity, owner_token)
                except IdempotencyStorageUnavailableError:
                    self._observer.observe_hot_degraded("abandon")
            raise

        await self._stop_heartbeat(heartbeat, stop_heartbeat)
        if lost_lease.is_set():
            self._observer.observe_hot_degraded("lease")
        if (
            not lost_lease.is_set()
            and result.completed is not None
            and result.outcome in {ExecutionOutcome.EXECUTED, ExecutionOutcome.REPLAY}
        ):
            try:
                await self._hot_store.complete(
                    identity,
                    owner_token,
                    result.completed,
                )
            except IdempotencyStorageUnavailableError:
                self._observer.observe_hot_degraded("complete")
        elif not lost_lease.is_set():
            try:
                await self._hot_store.abandon(identity, owner_token)
            except IdempotencyStorageUnavailableError:
                self._observer.observe_hot_degraded("abandon")
        return self._observe(result)

    def _observe(self, result: ExecutionResult) -> ExecutionResult:
        self._observer.observe_outcome(result.outcome.value)
        return result

    async def _execute_durable(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        operation: IdempotentOperation,
        *,
        prepare: IdempotencyPreparation | None,
    ) -> ExecutionResult:
        if prepare is not None:
            existing = await self._durable_execution.find_existing(
                identity,
                request_hash,
            )
            if existing is not None:
                return existing
            await prepare()
        return await self._durable_execution.execute_once(
            identity,
            request_hash,
            operation,
        )

    async def _heartbeat(
        self,
        *,
        identity: IdempotencyIdentity,
        owner_token: uuid.UUID,
        lease_seconds: int,
        lost_lease: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        interval = lease_seconds / 3
        while not stop.is_set():
            try:
                await self._sleeper.sleep(interval)
                if stop.is_set():
                    return
                renewed = await self._hot_store.renew(  # type: ignore[union-attr]
                    identity,
                    owner_token,
                    lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except IdempotencyStorageUnavailableError:
                lost_lease.set()
                return
            except Exception:
                # После старта durable path ошибка optional Redis heartbeat
                # не имеет права заменить уже закоммиченный DB outcome.
                lost_lease.set()
                return
            if not renewed:
                lost_lease.set()
                return

    @staticmethod
    async def _stop_heartbeat(
        heartbeat: asyncio.Task[None],
        stop: asyncio.Event,
    ) -> None:
        stop.set()
        if not heartbeat.done():
            heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
