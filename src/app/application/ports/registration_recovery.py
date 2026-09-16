from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from app.application.ports.events import EventPublisherProtocol, UserRegisteredEvent
from app.application.ports.repositories.registration_operations import (
    RegistrationOperationRepositoryProtocol,
)
from app.application.ports.repositories.users import UserRepositoryProtocol
from app.application.ports.uow import AsyncUOWProtocol


@dataclass(slots=True)
class RegistrationScope:
    """Fresh database scope for one short registration transaction."""

    uow: AsyncUOWProtocol
    registrations: RegistrationOperationRepositoryProtocol
    users: UserRepositoryProtocol
    events: EventPublisherProtocol[UserRegisteredEvent]


RegistrationScopeFactory = Callable[[], AbstractAsyncContextManager[RegistrationScope]]


class RegistrationObserverProtocol(Protocol):
    def operation_completed(self) -> None: ...

    def retry_scheduled(self, *, error_class: str) -> None: ...

    def operation_blocked(self, *, error_class: str) -> None: ...

    def lost_claim(self) -> None: ...

    def recovery_batch_claimed(self, *, count: int) -> None: ...
