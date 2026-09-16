from collections.abc import Callable
from datetime import datetime

from app.application.ports.registration_recovery import RegistrationScopeFactory
from app.application.use_cases.redrive_registration.command import (
    RedriveRegistrationCommand,
)
from app.domain.base import utc_now


class RedriveRegistrationHandler:
    def __init__(
        self,
        scope_factory: RegistrationScopeFactory,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._scope_factory = scope_factory
        self._clock = clock

    async def execute(self, command: RedriveRegistrationCommand) -> bool:
        async with self._scope_factory() as scope, scope.uow:
            return await scope.registrations.redrive_blocked(
                command.operation_id, available_at=self._clock()
            )
