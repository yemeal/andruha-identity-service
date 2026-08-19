from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from app.application.ports.repositories.outbox import OutboxRepositoryProtocol
from app.application.ports.uow import AsyncUOWProtocol


@dataclass(slots=True)
class OutboxScope:
    """Изолированный per-batch / per-message scope зависимостей БД."""

    uow: AsyncUOWProtocol
    outbox_repo: OutboxRepositoryProtocol


OutboxScopeFactory = Callable[[], AbstractAsyncContextManager[OutboxScope]]
