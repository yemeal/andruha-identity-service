from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
    ExecutionResult,
    StoredResult,
)
from app.application.value_objects.idempotency import IdempotencyIdentity

IdempotentOperation = Callable[[], Awaitable[StoredResult]]


class HotIdempotencyStoreProtocol(Protocol):
    async def begin(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        owner_token: UUID,
        lease_seconds: int,
    ) -> BeginResult: ...
    async def renew(
        self, identity: IdempotencyIdentity, owner_token: UUID, lease_seconds: int
    ) -> bool: ...
    async def complete(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
        result: CompletedIdempotencyResult,
    ) -> bool: ...
    async def abandon(
        self, identity: IdempotencyIdentity, owner_token: UUID
    ) -> bool: ...


class DurableExecutionProtocol(Protocol):
    async def find_existing(
        self, identity: IdempotencyIdentity, request_hash: bytes
    ) -> ExecutionResult | None: ...
    async def execute_once(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        operation: IdempotentOperation,
    ) -> ExecutionResult: ...


class IdempotencyCoordinatorProtocol(Protocol):
    async def execute(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        operation: IdempotentOperation,
        *,
        lease_seconds: int,
        prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> ExecutionResult: ...


class IdempotencyRecordRepositoryProtocol(Protocol):
    async def get_completed(
        self, identity: IdempotencyIdentity
    ) -> CompletedIdempotencyResult | None: ...
    async def try_add_completed(
        self, identity: IdempotencyIdentity, completed: CompletedIdempotencyResult
    ) -> bool: ...
    async def delete_expired(self, *, expires_at: datetime, limit: int) -> int: ...


class ReplayResultProtectorProtocol(Protocol):
    def protect(self, payload: dict[str, str], *, aad: bytes) -> dict[str, Any]: ...
    def restore(self, envelope: dict[str, Any], *, aad: bytes) -> dict[str, str]: ...


class IdempotencyObserverProtocol(Protocol):
    def observe_outcome(self, outcome: str) -> None: ...

    def observe_hot_degraded(self, stage: str) -> None: ...


class OwnerTokenFactory(Protocol):
    def __call__(self) -> UUID: ...


class AsyncSleeper(Protocol):
    async def sleep(self, seconds: float) -> None: ...


IdempotencyPreparation = Callable[[], Awaitable[None]]
