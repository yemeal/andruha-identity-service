from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from app.application.ports.dto.idempotency import StoredResult

IdempotentOperation = Callable[[], Awaitable[StoredResult]]
IdempotencyPreparation = Callable[[], Awaitable[None]]


class OwnerTokenFactory(Protocol):
    def __call__(self) -> UUID: ...


class AsyncSleeper(Protocol):
    async def sleep(self, seconds: float) -> None: ...
