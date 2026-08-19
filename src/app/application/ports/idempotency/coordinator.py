from collections.abc import Awaitable, Callable
from typing import Protocol

from app.application.ports.dto.idempotency import ExecutionResult
from app.application.ports.idempotency.types import IdempotentOperation
from app.application.value_objects.idempotency import IdempotencyIdentity


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
