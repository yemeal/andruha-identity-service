from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import random
import uuid

import structlog

from app.application.exceptions.persistence import (
    PersistenceUnavailableError,
    TransactionConflictError,
)
from app.application.exceptions.profiles import (
    ProfileProvisioningRejectedError,
    ProfileProvisioningUnavailableError,
)
from app.application.policies.registration_retry import RegistrationRetryPolicy
from app.application.ports.dto.registration import (
    RegistrationOperation,
    RegistrationOutcome,
    RegistrationResult,
    RegistrationStatus,
)
from app.application.ports.events import UserRegisteredEvent
from app.application.ports.profiles import ProfileProvisionerProtocol
from app.application.ports.registration_recovery import (
    RegistrationObserverProtocol,
    RegistrationScopeFactory,
)
from app.application.registration.results import pending_result
from app.application.use_cases.resume_registration.command import (
    ResumeRegistrationCommand,
)
from app.domain.aggregates.user import User
from app.domain.base import utc_now

logger = structlog.get_logger(__name__)


class _NullRegistrationObserver:
    def operation_completed(self) -> None:
        pass

    def retry_scheduled(self, *, error_class: str) -> None:
        pass

    def operation_blocked(self, *, error_class: str) -> None:
        pass

    def lost_claim(self) -> None:
        pass

    def recovery_batch_claimed(self, *, count: int) -> None:
        pass


class ResumeRegistrationHandler:
    def __init__(
        self,
        *,
        scope_factory: RegistrationScopeFactory,
        profile_provisioner: ProfileProvisionerProtocol,
        retry_policy: RegistrationRetryPolicy,
        observer: RegistrationObserverProtocol | None = None,
        clock: Callable[[], datetime] = utc_now,
        jitter_source: Callable[[], float] | None = None,
    ) -> None:
        self._scope_factory = scope_factory
        self._profile_provisioner = profile_provisioner
        self._retry_policy = retry_policy
        self._observer = observer or _NullRegistrationObserver()
        self._clock = clock
        self._jitter_source = jitter_source or (lambda: random.uniform(-1.0, 1.0))

    async def execute(self, command: ResumeRegistrationCommand) -> RegistrationResult:
        operation, owner_token = command.operation, command.owner_token
        if (
            operation.status is not RegistrationStatus.CLAIMED
            or operation.claim_token != owner_token
        ):
            raise ValueError("registration operation is not owned by this attempt")

        try:
            await self._profile_provisioner.create_profile(
                operation.user_id,
                operation.created_at,
            )
        except ProfileProvisioningRejectedError as error:
            await self._block(operation, owner_token=owner_token, error=error)
            raise ProfileProvisioningUnavailableError() from error
        except ProfileProvisioningUnavailableError as error:
            return await self._retry_or_block(
                operation,
                owner_token=owner_token,
                error=error,
            )
        except Exception as error:
            await self._block(operation, owner_token=owner_token, error=error)
            raise

        try:
            user = await self._finalize(operation, owner_token=owner_token)
        except _LostRegistrationClaim:
            self._observer.lost_claim()
            return pending_result(operation, now=self._clock())
        except _RegistrationFinalizationConflict as error:
            await self._block(operation, owner_token=owner_token, error=error)
            raise ProfileProvisioningUnavailableError() from error
        except (PersistenceUnavailableError, TransactionConflictError) as error:
            logger.exception(
                "registration finalization deferred",
                operation_id=str(operation.id),
            )
            return await self._retry_or_block(
                operation,
                owner_token=owner_token,
                error=error,
            )
        except Exception as error:
            await self._block(operation, owner_token=owner_token, error=error)
            raise

        self._observer.operation_completed()
        logger.info(
            "registration completed",
            operation_id=str(operation.id),
            user_id=str(user.id),
        )
        return RegistrationResult(
            operation_id=operation.id,
            user_id=user.id,
            outcome=RegistrationOutcome.COMPLETED,
        )

    async def _finalize(
        self,
        operation: RegistrationOperation,
        *,
        owner_token: uuid.UUID,
    ) -> User:
        password_hash = operation.password_hash
        if password_hash is None:
            raise RuntimeError("claimed registration has no password hash")
        completed_at = self._clock()
        user_to_create = User(
            id=operation.user_id,
            email=operation.email,
            password_hash=password_hash,
            created_at=operation.created_at,
        )
        async with self._scope_factory() as scope, scope.uow:
            if not await scope.registrations.complete(
                operation.id,
                owner_token=owner_token,
                completed_at=completed_at,
            ):
                raise _LostRegistrationClaim
            user = await scope.users.create_if_absent(user_to_create)
            if user is None:
                raise _RegistrationFinalizationConflict
            await scope.events.publish(
                UserRegisteredEvent(
                    user_id=user.id,
                    registered_at=user.created_at,
                )
            )
        return user

    async def _retry_or_block(
        self,
        operation: RegistrationOperation,
        *,
        owner_token: uuid.UUID,
        error: Exception,
    ) -> RegistrationResult:
        attempt = operation.attempts + 1
        if attempt >= self._retry_policy.max_attempts:
            await self._block(operation, owner_token=owner_token, error=error)
            raise ProfileProvisioningUnavailableError() from error

        failed_at = self._clock()
        delay = self._retry_policy.delay_seconds(
            attempt=attempt,
            jitter_sample=self._jitter_source(),
        )
        async with self._scope_factory() as scope, scope.uow:
            scheduled = await scope.registrations.schedule_retry(
                operation.id,
                owner_token=owner_token,
                failed_at=failed_at,
                available_at=failed_at + timedelta(seconds=delay),
                error_class=type(error).__name__,
            )
        if not scheduled:
            self._observer.lost_claim()
        else:
            self._observer.retry_scheduled(error_class=type(error).__name__)
        return pending_result(
            operation,
            now=self._clock(),
            retry_at=failed_at + timedelta(seconds=delay),
        )

    async def _block(
        self,
        operation: RegistrationOperation,
        *,
        owner_token: uuid.UUID,
        error: Exception,
    ) -> None:
        blocked_at = self._clock()
        async with self._scope_factory() as scope, scope.uow:
            blocked = await scope.registrations.block(
                operation.id,
                owner_token=owner_token,
                blocked_at=blocked_at,
                error_class=type(error).__name__,
            )
        if not blocked:
            self._observer.lost_claim()
            return
        self._observer.operation_blocked(error_class=type(error).__name__)


class _LostRegistrationClaim(Exception):
    pass


class _RegistrationFinalizationConflict(Exception):
    pass
