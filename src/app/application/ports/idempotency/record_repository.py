from datetime import datetime
from typing import Protocol

from app.application.ports.dto.idempotency import CompletedIdempotencyResult
from app.application.value_objects.idempotency import IdempotencyIdentity


class IdempotencyRecordRepositoryProtocol(Protocol):
    async def get_completed(
        self, identity: IdempotencyIdentity
    ) -> CompletedIdempotencyResult | None: ...

    async def try_add_completed(
        self, identity: IdempotencyIdentity, completed: CompletedIdempotencyResult
    ) -> bool: ...

    async def delete_expired(self, *, expires_at: datetime, limit: int) -> int: ...
