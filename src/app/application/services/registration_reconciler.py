from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
import uuid

import structlog

from app.application.exceptions.profiles import ProfileProvisioningUnavailableError
from app.application.ports.registration_recovery import (
    RegistrationObserverProtocol,
    RegistrationScopeFactory,
)
from app.application.services.registration import RegistrationAttemptProtocol
from app.domain.base import utc_now

logger = structlog.get_logger(__name__)


class RegistrationReconcilerProtocol(Protocol):
    async def run_once(self, batch_size: int = 25) -> int: ...

    async def run(self, poll_interval: float = 2.0, batch_size: int = 25) -> None: ...

    def stop(self) -> None: ...


class RegistrationReconcilerService(RegistrationReconcilerProtocol):
    def __init__(
        self,
        *,
        scope_factory: RegistrationScopeFactory,
        attempt: RegistrationAttemptProtocol,
        observer: RegistrationObserverProtocol,
        claim_lease_seconds: float,
        clock: Callable[[], datetime] = utc_now,
        owner_token_factory: Callable[[], uuid.UUID] = uuid.uuid7,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self._scope_factory = scope_factory
        self._attempt = attempt
        self._observer = observer
        self._claim_lease_seconds = claim_lease_seconds
        self._clock = clock
        self._owner_token_factory = owner_token_factory
        self._stop_event = asyncio.Event()

    async def run_once(self, batch_size: int = 25) -> int:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        owner_token = self._owner_token_factory()
        claimed_at = self._clock()
        async with self._scope_factory() as scope, scope.uow:
            operations = await scope.registrations.claim_batch(
                owner_token=owner_token,
                claimed_at=claimed_at,
                claim_expires_at=claimed_at
                + timedelta(seconds=self._claim_lease_seconds),
                limit=batch_size,
            )
        if not operations:
            return 0

        self._observer.recovery_batch_claimed(count=len(operations))

        async def process(operation) -> None:
            try:
                await self._attempt.resume_claimed(
                    operation,
                    owner_token=owner_token,
                )
            except ProfileProvisioningUnavailableError:
                logger.warning(
                    "registration recovery blocked",
                    operation_id=str(operation.id),
                )
            except Exception:
                # The claim expires and makes the operation recoverable again.
                logger.exception(
                    "registration recovery attempt failed",
                    operation_id=str(operation.id),
                )

        await asyncio.gather(
            *(process(operation) for operation in operations),
            return_exceptions=False,
        )
        return len(operations)

    async def run(self, poll_interval: float = 2.0, batch_size: int = 25) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._stop_event.clear()
        logger.info("registration_reconciler_started")
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once(batch_size=batch_size)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("registration_reconciler_iteration_failed")
                processed = 0

            if processed == 0 and not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=poll_interval,
                    )
                except TimeoutError:
                    continue
        logger.info("registration_reconciler_stopped")

    def stop(self) -> None:
        self._stop_event.set()
