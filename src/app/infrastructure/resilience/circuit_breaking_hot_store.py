from uuid import UUID

from app.application.exceptions.idempotency import IdempotencyStorageUnavailableError
from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
)
from app.application.ports.idempotency import HotIdempotencyStoreProtocol
from app.application.value_objects.idempotency import IdempotencyIdentity
from app.infrastructure.resilience import CircuitBreakerError, IdempotencyCircuitBreaker


class CircuitBreakingHotStore:
    def __init__(
        self,
        inner: HotIdempotencyStoreProtocol,
        circuit_breaker: IdempotencyCircuitBreaker,
    ) -> None:
        self._inner, self._circuit_breaker = inner, circuit_breaker

    async def begin(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        owner_token: UUID,
        lease_seconds: int,
    ) -> BeginResult:
        try:
            return await self._circuit_breaker.call(
                self._inner.begin,
                identity,
                request_hash,
                owner_token,
                lease_seconds,
            )
        except CircuitBreakerError as error:
            raise IdempotencyStorageUnavailableError() from error

    async def renew(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
        lease_seconds: int,
    ) -> bool:
        try:
            return await self._circuit_breaker.call(
                self._inner.renew, identity, owner_token, lease_seconds
            )
        except CircuitBreakerError as error:
            raise IdempotencyStorageUnavailableError() from error

    async def complete(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
        result: CompletedIdempotencyResult,
    ) -> bool:
        try:
            return await self._circuit_breaker.call(
                self._inner.complete, identity, owner_token, result
            )
        except CircuitBreakerError as error:
            raise IdempotencyStorageUnavailableError() from error

    async def abandon(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
    ) -> bool:
        try:
            return await self._circuit_breaker.call(
                self._inner.abandon, identity, owner_token
            )
        except CircuitBreakerError as error:
            raise IdempotencyStorageUnavailableError() from error
