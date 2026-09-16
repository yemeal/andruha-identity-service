from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from math import ceil
import random
from typing import Protocol
import uuid

import structlog

from app.application.exceptions.idempotency import IdempotencyKeyConflictError
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
from app.application.ports.security import PasswordHasherProtocol
from app.domain.base import utc_now
from app.domain.exceptions import DomainErrors
from app.domain.users import User
from app.domain.value_objects.email import NormalizedEmail

logger = structlog.get_logger(__name__)


class RegisterUserUseCaseProtocol(Protocol):
    async def execute(
        self,
        *,
        email: NormalizedEmail,
        password: str,
        key_hash: bytes,
    ) -> RegistrationResult: ...


class RegistrationAttemptProtocol(Protocol):
    async def resume_claimed(
        self,
        operation: RegistrationOperation,
        *,
        owner_token: uuid.UUID,
    ) -> RegistrationResult: ...


class RegistrationAdministrationProtocol(Protocol):
    async def redrive(self, operation_id: uuid.UUID) -> bool: ...


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


class RegistrationCoordinator(RegisterUserUseCaseProtocol, RegistrationAttemptProtocol):
    """Coordinates one durable registration without holding a DB transaction over HTTP."""

    def __init__(
        self,
        *,
        scope_factory: RegistrationScopeFactory,
        profile_provisioner: ProfileProvisionerProtocol,
        password_hasher: PasswordHasherProtocol,
        retry_policy: RegistrationRetryPolicy,
        claim_lease_seconds: float,
        observer: RegistrationObserverProtocol | None = None,
        clock: Callable[[], datetime] = utc_now,
        owner_token_factory: Callable[[], uuid.UUID] = uuid.uuid7,
        jitter_source: Callable[[], float] | None = None,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self._scope_factory = scope_factory
        self._profile_provisioner = profile_provisioner
        self._password_hasher = password_hasher
        self._retry_policy = retry_policy
        self._claim_lease_seconds = claim_lease_seconds
        self._observer = observer or _NullRegistrationObserver()
        self._clock = clock
        self._owner_token_factory = owner_token_factory
        self._jitter_source = jitter_source or (lambda: random.uniform(-1.0, 1.0))

    async def execute(
        self,
        *,
        email: NormalizedEmail,
        password: str,
        key_hash: bytes,
    ) -> RegistrationResult:
        if len(key_hash) != 32:
            raise ValueError("key_hash must be a full SHA-256 digest")

        existing = await self._load_by_key(key_hash)
        if existing is not None:
            return await self._resume_existing(existing, email=email, password=password)

        password_hash = await self._password_hasher.hash(password)
        now = self._clock()
        owner_token = self._owner_token_factory()
        candidate = RegistrationOperation(
            email=email,
            password_hash=password_hash,
            key_hash=key_hash,
            status=RegistrationStatus.CLAIMED,
            available_at=now,
            claim_token=owner_token,
            claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds),
            created_at=now,
        )

        async with self._scope_factory() as scope, scope.uow:
            if await scope.users.get_by_email(email) is not None:
                raise DomainErrors.User.EMAIL_ALREADY_EXISTS()
            created = await scope.registrations.try_create(candidate)

        if created is None:
            existing = await self._load_by_key(key_hash)
            if existing is None:
                raise DomainErrors.User.EMAIL_ALREADY_EXISTS()
            return await self._resume_existing(existing, email=email, password=password)

        return await self.resume_claimed(created, owner_token=owner_token)

    async def resume_claimed(
        self,
        operation: RegistrationOperation,
        *,
        owner_token: uuid.UUID,
    ) -> RegistrationResult:
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

        try:
            user = await self._finalize(operation, owner_token=owner_token)
        except _LostRegistrationClaim:
            self._observer.lost_claim()
            return self._pending_result(operation)
        except _RegistrationFinalizationConflict as error:
            await self._block(operation, owner_token=owner_token, error=error)
            raise ProfileProvisioningUnavailableError() from error
        except Exception as error:
            logger.exception(
                "registration finalization deferred",
                operation_id=str(operation.id),
            )
            return await self._retry_or_block(
                operation,
                owner_token=owner_token,
                error=error,
            )

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

    async def _resume_existing(
        self,
        operation: RegistrationOperation,
        *,
        email: NormalizedEmail,
        password: str,
    ) -> RegistrationResult:
        if operation.email != email:
            raise IdempotencyKeyConflictError()

        password_hash = operation.password_hash
        if password_hash is None:
            async with self._scope_factory() as scope, scope.uow:
                user = await scope.users.get(operation.user_id)
            if user is None:
                raise RuntimeError("completed registration has no user")
            password_hash = user.password_hash

        if not await self._password_hasher.verify(password, password_hash):
            raise IdempotencyKeyConflictError()

        if operation.status is RegistrationStatus.COMPLETED:
            return RegistrationResult(
                operation_id=operation.id,
                user_id=operation.user_id,
                outcome=RegistrationOutcome.COMPLETED,
            )
        if operation.status is RegistrationStatus.BLOCKED:
            raise ProfileProvisioningUnavailableError()

        now = self._clock()
        owner_token = self._owner_token_factory()
        async with self._scope_factory() as scope, scope.uow:
            claimed = await scope.registrations.claim_by_key_hash(
                operation.key_hash,
                owner_token=owner_token,
                claimed_at=now,
                claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds),
            )
        if claimed is None:
            return self._pending_result(operation)
        return await self.resume_claimed(claimed, owner_token=owner_token)

    async def _load_by_key(self, key_hash: bytes) -> RegistrationOperation | None:
        async with self._scope_factory() as scope, scope.uow:
            return await scope.registrations.get_by_key_hash(key_hash)

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
        return self._pending_result(
            operation,
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

    def _pending_result(
        self,
        operation: RegistrationOperation,
        *,
        retry_at: datetime | None = None,
    ) -> RegistrationResult:
        effective_retry_at = retry_at or operation.available_at
        if (
            retry_at is None
            and operation.status is RegistrationStatus.CLAIMED
            and operation.claim_expires_at is not None
        ):
            effective_retry_at = operation.claim_expires_at
        retry_after_seconds = max(
            1,
            ceil((effective_retry_at - self._clock()).total_seconds()),
        )
        return RegistrationResult(
            operation_id=operation.id,
            user_id=operation.user_id,
            outcome=RegistrationOutcome.PENDING,
            retry_after_seconds=retry_after_seconds,
        )


class _LostRegistrationClaim(Exception):
    pass


class _RegistrationFinalizationConflict(Exception):
    pass


class RegistrationAdministrationService(RegistrationAdministrationProtocol):
    def __init__(
        self,
        scope_factory: RegistrationScopeFactory,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._scope_factory = scope_factory
        self._clock = clock

    async def redrive(self, operation_id: uuid.UUID) -> bool:
        async with self._scope_factory() as scope, scope.uow:
            return await scope.registrations.redrive_blocked(
                operation_id,
                available_at=self._clock(),
            )
