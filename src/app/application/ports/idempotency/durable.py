from typing import Protocol

from app.application.ports.dto.idempotency import ExecutionResult
from app.application.ports.idempotency.types import IdempotentOperation
from app.application.value_objects.idempotency import IdempotencyIdentity


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
