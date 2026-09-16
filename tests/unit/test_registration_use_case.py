from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from types import TracebackType
import uuid

import pytest

from app.application.exceptions.idempotency import IdempotencyKeyConflictError
from app.application.exceptions.profiles import (
    ProfileProvisioningRejectedError,
    ProfileProvisioningUnavailableError,
)
from app.application.policies.registration_retry import RegistrationRetryPolicy
from app.application.ports.dto.registration import (
    RegistrationOperation,
    RegistrationOutcome,
    RegistrationStatus,
)
from app.application.ports.registration_recovery import (
    RegistrationScope,
    RegistrationScopeFactory,
)
from app.application.services.registration import (
    RegistrationAdministrationService,
    RegistrationCoordinator,
)
from app.application.services.registration_reconciler import (
    RegistrationReconcilerService,
)
from app.domain.exceptions import UserAlreadyExistsError
from app.domain.users import User


class Store:
    def __init__(self) -> None:
        self.active_transactions = 0
        self.commits = 0
        self.rollbacks = 0
        self.operations: dict[uuid.UUID, RegistrationOperation] = {}
        self.users: dict[uuid.UUID, User] = {}
        self.events: list[object] = []


class UOW:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def __aenter__(self) -> UOW:
        self.store.active_transactions += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        self.store.active_transactions -= 1
        if exc_type is None:
            self.store.commits += 1
        else:
            self.store.rollbacks += 1


class Users:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def get(self, user_id: uuid.UUID) -> User | None:
        return self.store.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return next(
            (user for user in self.store.users.values() if user.email == email), None
        )

    async def create_if_absent(self, user: User) -> User | None:
        if await self.get_by_email(user.email) is not None:
            return None
        self.store.users[user.id] = user
        return user


class Events:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def publish(self, event: object) -> None:
        self.store.events.append(event)


class Registrations:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def try_create(
        self, operation: RegistrationOperation
    ) -> RegistrationOperation | None:
        if any(
            item.key_hash == operation.key_hash or item.email == operation.email
            for item in self.store.operations.values()
        ):
            return None
        self.store.operations[operation.id] = operation
        return operation

    async def get_by_key_hash(self, key_hash: bytes) -> RegistrationOperation | None:
        return next(
            (
                item
                for item in self.store.operations.values()
                if item.key_hash == key_hash
            ),
            None,
        )

    async def get_by_email(self, email: str) -> RegistrationOperation | None:
        return next(
            (item for item in self.store.operations.values() if item.email == email),
            None,
        )

    async def claim_by_key_hash(
        self,
        key_hash: bytes,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> RegistrationOperation | None:
        operation = await self.get_by_key_hash(key_hash)
        if operation is None or not self._eligible(operation, claimed_at):
            return None
        claimed = operation.model_copy(
            update={
                "status": RegistrationStatus.CLAIMED,
                "claim_token": owner_token,
                "claim_expires_at": claim_expires_at,
            }
        )
        self.store.operations[claimed.id] = claimed
        return claimed

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[RegistrationOperation]:
        claimed: list[RegistrationOperation] = []
        for operation in list(self.store.operations.values()):
            if len(claimed) >= limit or not self._eligible(operation, claimed_at):
                continue
            item = operation.model_copy(
                update={
                    "status": RegistrationStatus.CLAIMED,
                    "claim_token": owner_token,
                    "claim_expires_at": claim_expires_at,
                }
            )
            self.store.operations[item.id] = item
            claimed.append(item)
        return claimed

    async def complete(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        completed_at: datetime,
    ) -> bool:
        operation = self.store.operations[operation_id]
        if not self._owns(operation, owner_token, completed_at):
            return False
        self.store.operations[operation_id] = operation.model_copy(
            update={
                "status": RegistrationStatus.COMPLETED,
                "attempts": operation.attempts + 1,
                "password_hash": None,
                "claim_token": None,
                "claim_expires_at": None,
                "terminal_at": completed_at,
            }
        )
        return True

    async def schedule_retry(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        failed_at: datetime,
        available_at: datetime,
        error_class: str,
    ) -> bool:
        operation = self.store.operations[operation_id]
        if not self._owns(operation, owner_token, failed_at):
            return False
        self.store.operations[operation_id] = operation.model_copy(
            update={
                "status": RegistrationStatus.PENDING,
                "attempts": operation.attempts + 1,
                "available_at": available_at,
                "last_error_class": error_class,
                "claim_token": None,
                "claim_expires_at": None,
            }
        )
        return True

    async def block(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        blocked_at: datetime,
        error_class: str,
    ) -> bool:
        operation = self.store.operations[operation_id]
        if not self._owns(operation, owner_token, blocked_at):
            return False
        self.store.operations[operation_id] = operation.model_copy(
            update={
                "status": RegistrationStatus.BLOCKED,
                "attempts": operation.attempts + 1,
                "last_error_class": error_class,
                "claim_token": None,
                "claim_expires_at": None,
                "terminal_at": blocked_at,
            }
        )
        return True

    async def redrive_blocked(
        self,
        operation_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool:
        operation = self.store.operations[operation_id]
        if operation.status is not RegistrationStatus.BLOCKED:
            return False
        self.store.operations[operation_id] = operation.model_copy(
            update={
                "status": RegistrationStatus.PENDING,
                "attempts": 0,
                "last_error_class": None,
                "available_at": available_at,
                "terminal_at": None,
                "redrive_count": operation.redrive_count + 1,
            }
        )
        return True

    @staticmethod
    def _eligible(operation: RegistrationOperation, now: datetime) -> bool:
        return (
            operation.status is RegistrationStatus.PENDING
            and operation.available_at <= now
        ) or (
            operation.status is RegistrationStatus.CLAIMED
            and operation.claim_expires_at is not None
            and operation.claim_expires_at <= now
        )

    @staticmethod
    def _owns(
        operation: RegistrationOperation, owner_token: uuid.UUID, now: datetime
    ) -> bool:
        return (
            operation.status is RegistrationStatus.CLAIMED
            and operation.claim_token == owner_token
            and operation.claim_expires_at is not None
            and operation.claim_expires_at > now
        )


class Hasher:
    async def hash(self, password: str) -> str:
        return f"argon2:{password}"

    async def verify(self, password: str, password_hash: str) -> bool:
        return password_hash == f"argon2:{password}"

    async def verify_or_dummy(self, password: str, password_hash: str | None) -> bool:
        return password_hash == f"argon2:{password}"


class Profile:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[tuple[uuid.UUID, datetime]] = []
        self.failure: Exception | None = None

    async def create_profile(self, user_id: uuid.UUID, registered_at: datetime) -> None:
        assert self.store.active_transactions == 0
        self.calls.append((user_id, registered_at))
        if self.failure is not None:
            raise self.failure


class Observer:
    def __init__(self) -> None:
        self.completed = 0
        self.retries = 0
        self.blocked = 0
        self.lost = 0
        self.claimed = 0

    def operation_completed(self) -> None:
        self.completed += 1

    def retry_scheduled(self, *, error_class: str) -> None:
        self.retries += 1

    def operation_blocked(self, *, error_class: str) -> None:
        self.blocked += 1

    def lost_claim(self) -> None:
        self.lost += 1

    def recovery_batch_claimed(self, *, count: int) -> None:
        self.claimed += count


@dataclass
class Scenario:
    now: datetime
    store: Store
    registrations: Registrations
    profile: Profile
    observer: Observer
    scope_factory: RegistrationScopeFactory
    coordinator: RegistrationCoordinator


def make_scenario(*, max_attempts: int = 3) -> Scenario:
    now = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    store = Store()
    registrations = Registrations(store)
    users = Users(store)
    events = Events(store)

    @asynccontextmanager
    async def scope_factory():
        yield RegistrationScope(
            uow=UOW(store),
            registrations=registrations,
            users=users,
            events=events,
        )

    profile = Profile(store)
    observer = Observer()
    coordinator = RegistrationCoordinator(
        scope_factory=scope_factory,
        profile_provisioner=profile,
        password_hasher=Hasher(),
        retry_policy=RegistrationRetryPolicy(
            initial_seconds=1,
            max_seconds=10,
            jitter_ratio=0,
            max_attempts=max_attempts,
        ),
        claim_lease_seconds=30,
        observer=observer,
        clock=lambda: now,
        jitter_source=lambda: 0,
    )
    return Scenario(
        now,
        store,
        registrations,
        profile,
        observer,
        scope_factory,
        coordinator,
    )


def key_hash(value: str = "registration-key") -> bytes:
    return hashlib.sha256(value.encode()).digest()


async def test_success_creates_user_only_after_profile_outside_transaction() -> None:
    scenario = make_scenario()

    result = await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash(),
    )

    assert result.outcome is RegistrationOutcome.COMPLETED
    assert scenario.profile.calls == [(result.user_id, scenario.now)]
    assert scenario.store.users[result.user_id].email == "new@example.com"
    assert len(scenario.store.events) == 1
    operation = scenario.store.operations[result.operation_id]
    assert operation.status is RegistrationStatus.COMPLETED
    assert operation.password_hash is None
    assert scenario.observer.completed == 1


async def test_retryable_profile_failure_is_durable_but_creates_no_user() -> None:
    scenario = make_scenario()
    scenario.profile.failure = ProfileProvisioningUnavailableError()

    result = await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash(),
    )

    assert result.outcome is RegistrationOutcome.PENDING
    assert result.retry_after_seconds == 1
    assert scenario.store.users == {}
    assert scenario.store.events == []
    operation = scenario.store.operations[result.operation_id]
    assert operation.status is RegistrationStatus.PENDING
    assert operation.attempts == 1
    assert operation.available_at == scenario.now + timedelta(seconds=1)
    assert scenario.observer.retries == 1


async def test_permanent_profile_rejection_blocks_operation() -> None:
    scenario = make_scenario()
    scenario.profile.failure = ProfileProvisioningRejectedError()

    with pytest.raises(ProfileProvisioningUnavailableError):
        await scenario.coordinator.execute(
            email="new@example.com",
            password="password",
            key_hash=key_hash(),
        )

    operation = next(iter(scenario.store.operations.values()))
    assert operation.status is RegistrationStatus.BLOCKED
    assert scenario.store.users == {}
    assert scenario.observer.blocked == 1


async def test_completed_key_replays_without_second_profile_call() -> None:
    scenario = make_scenario()
    first = await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash(),
    )

    replay = await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash(),
    )

    assert replay == first
    assert len(scenario.profile.calls) == 1


async def test_same_key_with_different_password_conflicts() -> None:
    scenario = make_scenario()
    await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash(),
    )

    with pytest.raises(IdempotencyKeyConflictError):
        await scenario.coordinator.execute(
            email="new@example.com",
            password="another-password",
            key_hash=key_hash(),
        )


async def test_different_key_for_registered_email_conflicts() -> None:
    scenario = make_scenario()
    await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash("first-key"),
    )

    with pytest.raises(UserAlreadyExistsError):
        await scenario.coordinator.execute(
            email="new@example.com",
            password="password",
            key_hash=key_hash("second-key"),
        )


async def test_reconciler_completes_due_pending_operation() -> None:
    scenario = make_scenario()
    scenario.profile.failure = ProfileProvisioningUnavailableError()
    pending = await scenario.coordinator.execute(
        email="new@example.com",
        password="password",
        key_hash=key_hash(),
    )
    operation = scenario.store.operations[pending.operation_id]
    scenario.store.operations[operation.id] = operation.model_copy(
        update={"available_at": scenario.now}
    )
    scenario.profile.failure = None

    reconciler = RegistrationReconcilerService(
        scope_factory=scenario.scope_factory,
        attempt=scenario.coordinator,
        observer=scenario.observer,
        claim_lease_seconds=30,
        clock=lambda: scenario.now,
    )
    assert await reconciler.run_once() == 1

    assert scenario.store.operations[pending.operation_id].status is (
        RegistrationStatus.COMPLETED
    )
    assert pending.user_id in scenario.store.users
    assert scenario.observer.claimed == 1


async def test_blocked_operation_can_be_explicitly_redriven() -> None:
    scenario = make_scenario()
    scenario.profile.failure = ProfileProvisioningRejectedError()
    with pytest.raises(ProfileProvisioningUnavailableError):
        await scenario.coordinator.execute(
            email="new@example.com",
            password="password",
            key_hash=key_hash(),
        )
    operation = next(iter(scenario.store.operations.values()))

    administration = RegistrationAdministrationService(
        scenario.scope_factory,
        clock=lambda: scenario.now,
    )
    assert await administration.redrive(operation.id)

    redriven = scenario.store.operations[operation.id]
    assert redriven.status is RegistrationStatus.PENDING
    assert redriven.attempts == 0
    assert redriven.terminal_at is None
    assert redriven.redrive_count == 1
