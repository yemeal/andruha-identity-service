from typing import Protocol
from uuid import UUID

from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
)
from app.application.value_objects.idempotency import IdempotencyIdentity


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
