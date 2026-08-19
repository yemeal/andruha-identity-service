import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from app.application.exceptions import (
    PermanentPublishError,
    TransientPublishError,
)
from app.application.policies.outbox_retry import OutboxRetryPolicy
from app.application.ports.dto.outbox import (
    OutboxKind,
    OutboxMessage,
    OutboxStatus,
)
from app.application.ports.events.publisher import BrokerPublisherProtocol
from app.application.ports.outbox.observer import OutboxRelayObserverProtocol
from app.application.ports.outbox.scope_factory import (
    OutboxScope,
    OutboxScopeFactory,
)
from app.application.ports.repositories.outbox import OutboxRepositoryProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.services.outbox_relay import OutboxRelayService

# =============================================================================
# Test Doubles / Fakes
# =============================================================================


class FakeUOW(AsyncUOWProtocol):
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> FakeUOW:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            self.rolled_back = True
        else:
            self.committed = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeOutboxRepository(OutboxRepositoryProtocol):
    def __init__(self, initial_messages: list[OutboxMessage] | None = None) -> None:
        self.messages: dict[uuid.UUID, OutboxMessage] = {
            m.id: m for m in (initial_messages or [])
        }
        self.finalized_ids: list[uuid.UUID] = []
        self.retried_ids: list[tuple[uuid.UUID, datetime, str]] = []
        self.quarantined_ids: list[tuple[uuid.UUID, datetime, str]] = []

    async def create(self, entity: OutboxMessage) -> OutboxMessage:
        self.messages[entity.id] = entity
        return entity

    async def get(self, entity_id: uuid.UUID) -> OutboxMessage | None:
        return self.messages.get(entity_id)

    async def update(self, entity: OutboxMessage) -> OutboxMessage:
        self.messages[entity.id] = entity
        return entity

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[OutboxMessage]:
        claimed: list[OutboxMessage] = []
        for m in list(self.messages.values()):
            if m.status in (OutboxStatus.PENDING, OutboxStatus.CLAIMED):
                updated = m.model_copy(
                    update={
                        "status": OutboxStatus.CLAIMED,
                        "claim_token": owner_token,
                        "claim_expires_at": claim_expires_at,
                    }
                )
                self.messages[m.id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
        return claimed

    async def finalize_published(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        published_at: datetime,
    ) -> bool:
        if message_id in self.messages:
            self.finalized_ids.append(message_id)
            self.messages[message_id] = self.messages[message_id].model_copy(
                update={
                    "status": OutboxStatus.SUCCESS,
                    "terminal_at": published_at,
                    "claim_token": None,
                    "claim_expires_at": None,
                    "attempts": self.messages[message_id].attempts + 1,
                }
            )
            return True
        return False

    async def schedule_retry(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        available_at: datetime,
        error_class: str = "TransientPublishError",
    ) -> bool:
        if message_id in self.messages:
            self.retried_ids.append((message_id, available_at, error_class))
            self.messages[message_id] = self.messages[message_id].model_copy(
                update={
                    "status": OutboxStatus.PENDING,
                    "available_at": available_at,
                    "last_error_class": error_class,
                    "claim_token": None,
                    "claim_expires_at": None,
                    "attempts": self.messages[message_id].attempts + 1,
                }
            )
            return True
        return False

    async def quarantine(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        quarantined_at: datetime,
        error_class: str = "PermanentOutboxPublishError",
    ) -> bool:
        if message_id in self.messages:
            self.quarantined_ids.append((message_id, quarantined_at, error_class))
            self.messages[message_id] = self.messages[message_id].model_copy(
                update={
                    "status": OutboxStatus.QUARANTINED,
                    "terminal_at": quarantined_at,
                    "last_error_class": error_class,
                    "claim_token": None,
                    "claim_expires_at": None,
                    "attempts": self.messages[message_id].attempts + 1,
                }
            )
            return True
        return False

    async def redrive_quarantined(
        self,
        message_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool:
        return True

    async def delete_terminal_before(
        self,
        *,
        terminal_at: datetime,
        limit: int,
    ) -> int:
        return 0


class FakeBrokerPublisher(BrokerPublisherProtocol):
    def __init__(self) -> None:
        self.published: list[OutboxMessage] = []
        self.fail_mode: dict[uuid.UUID, Exception] = {}
        self.publish_delays: dict[uuid.UUID, float] = {}

    async def publish(self, message: OutboxMessage) -> None:
        if message.id in self.publish_delays:
            await asyncio.sleep(self.publish_delays[message.id])
        if message.id in self.fail_mode:
            raise self.fail_mode[message.id]
        self.published.append(message)


class FakeObserver(OutboxRelayObserverProtocol):
    def __init__(self) -> None:
        self.batches_claimed: list[int] = []
        self.finalized_events: list[uuid.UUID] = []
        self.retried_events: list[uuid.UUID] = []
        self.quarantined_events: list[tuple[uuid.UUID, str]] = []

    def batch_claimed(
        self, *, count: int, oldest_created_at: datetime, observed_at: datetime
    ) -> None:
        self.batches_claimed.append(count)

    def message_finalized(self, *, message_id: uuid.UUID, message_type: str) -> None:
        self.finalized_events.append(message_id)

    def message_retry_scheduled(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
        attempt: int,
        delay_seconds: float,
    ) -> None:
        self.retried_events.append(message_id)

    def message_quarantined(
        self, *, message_id: uuid.UUID, message_type: str, error_class: str
    ) -> None:
        self.quarantined_events.append((message_id, error_class))


def make_outbox_message(
    *,
    key: str = "user-1",
    status: OutboxStatus = OutboxStatus.PENDING,
    attempts: int = 0,
) -> OutboxMessage:
    now = datetime.now(UTC)
    return OutboxMessage(
        id=uuid.uuid7(),
        kind=OutboxKind.EVENT,
        topic="identity.events.v1",
        key=key,
        type="identity.user_registered.v1",
        payload={"userId": key},
        status=status,
        attempts=attempts,
        available_at=now,
        created_at=now,
    )


def make_scope_factory(repo: FakeOutboxRepository) -> OutboxScopeFactory:
    @asynccontextmanager
    async def factory():
        yield OutboxScope(uow=FakeUOW(), outbox_repo=repo)

    return factory


# =============================================================================
# Unit Tests for OutboxRetryPolicy
# =============================================================================


def test_retry_policy_exponential_growth():
    policy = OutboxRetryPolicy(
        initial_seconds=1.0,
        max_seconds=60.0,
        exponent=2.0,
        jitter_ratio=0.0,
    )
    assert policy.delay_seconds(attempt=1, jitter_sample=0.0) == 1.0
    assert policy.delay_seconds(attempt=2, jitter_sample=0.0) == 2.0
    assert policy.delay_seconds(attempt=3, jitter_sample=0.0) == 4.0
    assert policy.delay_seconds(attempt=4, jitter_sample=0.0) == 8.0


def test_retry_policy_max_cap():
    policy = OutboxRetryPolicy(
        initial_seconds=1.0,
        max_seconds=10.0,
        exponent=2.0,
        jitter_ratio=0.0,
    )
    assert policy.delay_seconds(attempt=10, jitter_sample=0.0) == 10.0


def test_retry_policy_jitter_boundaries():
    policy = OutboxRetryPolicy(
        initial_seconds=10.0,
        max_seconds=60.0,
        exponent=2.0,
        jitter_ratio=0.2,
    )
    # jitter_sample = -1 -> -20% -> 8.0
    assert policy.delay_seconds(attempt=1, jitter_sample=-1.0) == 8.0
    # jitter_sample = +1 -> +20% -> 12.0
    assert policy.delay_seconds(attempt=1, jitter_sample=1.0) == 12.0


# =============================================================================
# Unit Tests for OutboxRelayService
# =============================================================================


@pytest.mark.asyncio
async def test_relay_happy_path_single_message():
    msg = make_outbox_message()
    repo = FakeOutboxRepository([msg])
    publisher = FakeBrokerPublisher()
    observer = FakeObserver()

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
        observer=observer,
    )

    processed = await relay.run_once(batch_size=10)

    assert processed == 1
    assert len(publisher.published) == 1
    assert publisher.published[0].id == msg.id
    assert msg.id in repo.finalized_ids
    assert repo.messages[msg.id].status == OutboxStatus.SUCCESS
    assert msg.id in observer.finalized_events


@pytest.mark.asyncio
async def test_relay_concurrent_publishing_across_different_keys():
    msg1 = make_outbox_message(key="user-1")
    msg2 = make_outbox_message(key="user-2")
    msg3 = make_outbox_message(key="user-3")
    repo = FakeOutboxRepository([msg1, msg2, msg3])
    publisher = FakeBrokerPublisher()
    # Add small delay to simulate network I/O
    for m in (msg1, msg2, msg3):
        publisher.publish_delays[m.id] = 0.05

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
    )

    start = asyncio.get_event_loop().time()
    processed = await relay.run_once(batch_size=10)
    elapsed = asyncio.get_event_loop().time() - start

    assert processed == 3
    assert len(publisher.published) == 3
    # Concurrent execution across 3 keys should take ~0.05-0.08s, NOT 0.15s (sequential)
    assert elapsed < 0.12


@pytest.mark.asyncio
async def test_relay_sequential_fifo_within_same_key():
    msg1 = make_outbox_message(key="user-same")
    msg2 = make_outbox_message(key="user-same")
    repo = FakeOutboxRepository([msg1, msg2])
    publisher = FakeBrokerPublisher()

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
    )

    processed = await relay.run_once(batch_size=10)

    assert processed == 2
    assert [m.id for m in publisher.published] == [msg1.id, msg2.id]


@pytest.mark.asyncio
async def test_relay_transient_error_schedules_retry():
    msg = make_outbox_message(attempts=0)
    repo = FakeOutboxRepository([msg])
    publisher = FakeBrokerPublisher()
    publisher.fail_mode[msg.id] = TransientPublishError("Network timeout")
    observer = FakeObserver()

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
        observer=observer,
    )

    processed = await relay.run_once(batch_size=10)

    assert processed == 1
    assert len(publisher.published) == 0
    assert len(repo.retried_ids) == 1
    retried_id, _available_at, error_class = repo.retried_ids[0]
    assert retried_id == msg.id
    assert error_class == "TransientPublishError"
    assert repo.messages[msg.id].status == OutboxStatus.PENDING
    assert repo.messages[msg.id].attempts == 1
    assert msg.id in observer.retried_events


@pytest.mark.asyncio
async def test_relay_permanent_error_quarantines_message():
    msg = make_outbox_message(attempts=0)
    repo = FakeOutboxRepository([msg])
    publisher = FakeBrokerPublisher()
    publisher.fail_mode[msg.id] = PermanentPublishError("Invalid schema payload")
    observer = FakeObserver()

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
        observer=observer,
    )

    processed = await relay.run_once(batch_size=10)

    assert processed == 1
    assert len(publisher.published) == 0
    assert len(repo.quarantined_ids) == 1
    quarantined_id, _quarantined_at, error_class = repo.quarantined_ids[0]
    assert quarantined_id == msg.id
    assert error_class == "PermanentPublishError"
    assert repo.messages[msg.id].status == OutboxStatus.QUARANTINED
    assert repo.messages[msg.id].attempts == 1
    assert (msg.id, "PermanentPublishError") in observer.quarantined_events


@pytest.mark.asyncio
async def test_relay_retry_exhaustion_routes_to_quarantine():
    # Message already attempted 9 times, policy max_attempts = 10
    msg = make_outbox_message(attempts=9)
    repo = FakeOutboxRepository([msg])
    publisher = FakeBrokerPublisher()
    # Transient error on 10th attempt should exceed limit
    publisher.fail_mode[msg.id] = TransientPublishError("Kafka unavailable")

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
        retry_policy=OutboxRetryPolicy(max_attempts=10),
    )

    processed = await relay.run_once(batch_size=10)

    assert processed == 1
    assert len(repo.quarantined_ids) == 1
    quarantined_id, _, error_class = repo.quarantined_ids[0]
    assert quarantined_id == msg.id
    assert error_class == "RetryExhaustedError"
    assert repo.messages[msg.id].status == OutboxStatus.QUARANTINED


@pytest.mark.asyncio
async def test_relay_graceful_stop():
    repo = FakeOutboxRepository([])
    publisher = FakeBrokerPublisher()

    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=make_scope_factory(repo),
    )

    # Start relay background task
    task = asyncio.create_task(relay.run(poll_interval=0.1, batch_size=10))
    await asyncio.sleep(0.05)
    relay.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
