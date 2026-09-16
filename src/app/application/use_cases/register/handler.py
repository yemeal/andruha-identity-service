from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import uuid

from app.application.exceptions.idempotency import IdempotencyKeyConflictError
from app.application.exceptions.profiles import (
    ProfileProvisioningUnavailableError,
)
from app.application.ports.dto.registration import (
    RegistrationOperation,
    RegistrationOutcome,
    RegistrationResult,
    RegistrationStatus,
)
from app.application.ports.registration_recovery import (
    RegistrationScopeFactory,
)
from app.application.ports.security import PasswordHasherProtocol
from app.application.registration.results import pending_result
from app.application.use_cases.register.command import RegisterUserCommand
from app.application.use_cases.resume_registration.command import (
    ResumeRegistrationCommand,
)
from app.application.use_cases.resume_registration.handler import (
    ResumeRegistrationHandler,
)
from app.domain.base import utc_now
from app.domain.exceptions import UserAlreadyExistsError
from app.domain.value_objects.email import NormalizedEmail


class RegisterUserHandler:
    def __init__(
        self,
        *,
        scope_factory: RegistrationScopeFactory,
        password_hasher: PasswordHasherProtocol,
        resume: ResumeRegistrationHandler,
        claim_lease_seconds: float,
        clock: Callable[[], datetime] = utc_now,
        owner_token_factory: Callable[[], uuid.UUID] = uuid.uuid7,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self._scope_factory = scope_factory
        self._password_hasher = password_hasher
        self._resume = resume
        self._claim_lease_seconds = claim_lease_seconds
        self._clock = clock
        self._owner_token_factory = owner_token_factory

    async def execute(self, command: RegisterUserCommand) -> RegistrationResult:
        email, password, key_hash = command.email, command.password, command.key_hash
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
            user = await scope.users.get_by_email(email)
            created = (
                await scope.registrations.try_create(candidate)
                if user is None
                else None
            )

        if created is None:
            existing = await self._load_by_key(key_hash)
            if existing is None:
                raise UserAlreadyExistsError()
            return await self._resume_existing(existing, email=email, password=password)

        return await self._resume.execute(
            ResumeRegistrationCommand(operation=created, owner_token=owner_token)
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
            return pending_result(operation, now=self._clock())
        return await self._resume.execute(
            ResumeRegistrationCommand(operation=claimed, owner_token=owner_token)
        )

    async def _load_by_key(self, key_hash: bytes) -> RegistrationOperation | None:
        async with self._scope_factory() as scope, scope.uow:
            return await scope.registrations.get_by_key_hash(key_hash)
