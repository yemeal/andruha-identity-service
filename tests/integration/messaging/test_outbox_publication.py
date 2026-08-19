from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration.helpers import register

from app.application.exceptions import (
    PermanentPublishError,
    TransientPublishError,
)
from app.application.ports.dto.outbox import (
    OutboxKind,
    OutboxMessage,
    OutboxStatus,
)
from app.application.ports.events.publisher import BrokerPublisherProtocol
from app.application.ports.outbox.scope_factory import (
    OutboxScope,
    OutboxScopeFactory,
)
from app.application.services.outbox_relay import (
    OutboxRelayService,
    OutboxRetryPolicy,
)
from app.domain.base import utc_now
from app.infrastructure.database.models import OutboxMessageORM, UserORM
from app.infrastructure.database.repositories.outbox_repository import (
    OutboxRepository,
)
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW

pytestmark = pytest.mark.integration


# =============================================================================
# Test Doubles
# =============================================================================


class RecordingBrokerPublisher(BrokerPublisherProtocol):
    """Сборщик опубликованных сообщений для интеграционных тестов."""

    def __init__(self) -> None:
        self.published: list[OutboxMessage] = []
        self.fail_rules: dict[uuid.UUID, Exception] = {}
        self.delays: dict[uuid.UUID, float] = {}

    async def publish(self, message: OutboxMessage) -> None:
        if message.id in self.delays:
            await asyncio.sleep(self.delays[message.id])
        if message.id in self.fail_rules:
            raise self.fail_rules[message.id]
        self.published.append(message)


def build_scope_factory(
    sessionmaker: async_sessionmaker[AsyncSession],
    topic: str = "identity.user-registered.v1",
    producer: str = "andruha-identity-service",
) -> OutboxScopeFactory:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        async with sessionmaker() as session:
            uow = SQLAlchemyAsyncUOW(session)
            repo = OutboxRepository(session=session, topic=topic, producer=producer)
            yield OutboxScope(uow=uow, outbox_repo=repo)

    return factory


# =============================================================================
# Integration Tests
# =============================================================================


async def test_registration_atomically_persists_user_and_outbox_message(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    """Проверяет атомарную запись пользователя и outbox-события в одной транзакции."""
    email, response = register(identity_client)
    assert response.status_code == 201
    user_id = response.json()["userId"]

    # 1. Проверяем наличие пользователя в БД
    user = await database_session.scalar(select(UserORM).where(UserORM.email == email))
    assert user is not None
    assert str(user.id) == user_id

    # 2. Проверяем наличие outbox-события в БД
    outbox_orm = await database_session.scalar(
        select(OutboxMessageORM).where(OutboxMessageORM.key == user_id)
    )
    assert outbox_orm is not None
    assert outbox_orm.status == OutboxStatus.PENDING
    assert outbox_orm.kind == OutboxKind.EVENT
    assert outbox_orm.type == "identity.user_registered.v1"
    assert outbox_orm.topic == "identity.user-registered.v1"
    assert outbox_orm.attempts == 0
    assert outbox_orm.claim_token is None
    assert outbox_orm.claim_expires_at is None
    assert outbox_orm.terminal_at is None

    # 3. Проверяем валидность контракта Envelope
    payload = outbox_orm.payload
    assert payload["eventType"] == "identity.user_registered.v1"
    assert payload["producer"] == "andruha-identity-service"
    assert payload["payload"]["userId"] == user_id


async def test_duplicate_registration_rolls_back_and_persists_no_outbox(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    """Проверяет откат outbox-события при сбое регистрации (дубликат email)."""
    email, resp1 = register(identity_client)
    assert resp1.status_code == 201

    # Вторая попытка с тем же email
    resp2 = register(identity_client, email=email)[1]
    assert resp2.status_code == 409

    # В outbox должна остаться ровно 1 запись от первой успешной регистрации
    outbox_count = len(
        (
            await database_session.scalars(
                select(OutboxMessageORM).where(
                    OutboxMessageORM.type == "identity.user_registered.v1"
                )
            )
        ).all()
    )
    assert outbox_count == 1


async def test_relay_claims_and_finalizes_outbox_messages(
    identity_client: TestClient,
    database_sessionmaker: async_sessionmaker[AsyncSession],
    database_session: AsyncSession,
) -> None:
    """Проверяет полный цикл обработки релеем: захват -> публикация -> финализация."""
    # Создаем 3 пользователей (3 события в outbox)
    for _ in range(3):
        register(identity_client)

    publisher = RecordingBrokerPublisher()
    scope_factory = build_scope_factory(database_sessionmaker)
    relay = OutboxRelayService(publisher=publisher, scope_factory=scope_factory)

    # Запускаем одну итерацию релея
    processed = await relay.run_once(batch_size=10)
    assert processed == 3
    assert len(publisher.published) == 3

    # Проверяем состояние строк в БД после релея
    for msg in publisher.published:
        outbox_row = await database_session.scalar(
            select(OutboxMessageORM).where(OutboxMessageORM.id == msg.id)
        )
        assert outbox_row is not None
        assert outbox_row.status == OutboxStatus.SUCCESS
        assert outbox_row.terminal_at is not None
        assert outbox_row.claim_token is None
        assert outbox_row.claim_expires_at is None
        assert outbox_row.attempts == 1


async def test_relay_transient_failure_triggers_backoff_in_database(
    identity_client: TestClient,
    database_sessionmaker: async_sessionmaker[AsyncSession],
    database_session: AsyncSession,
) -> None:
    """Проверяет, что при сбое брокера запись возвращается в PENDING с бэкоффом."""
    _, response = register(identity_client)
    user_id = response.json()["userId"]

    outbox_row = await database_session.scalar(
        select(OutboxMessageORM).where(OutboxMessageORM.key == user_id)
    )
    assert outbox_row is not None

    publisher = RecordingBrokerPublisher()
    publisher.fail_rules[outbox_row.id] = TransientPublishError(
        "Kafka partition timeout"
    )

    scope_factory = build_scope_factory(database_sessionmaker)
    relay = OutboxRelayService(
        publisher=publisher,
        scope_factory=scope_factory,
        retry_policy=OutboxRetryPolicy(initial_seconds=5.0, jitter_ratio=0.0),
    )

    processed = await relay.run_once(batch_size=10)
    assert processed == 1
    assert len(publisher.published) == 0

    # Обновляем состояние из БД
    await database_session.refresh(outbox_row)
    assert outbox_row.status == OutboxStatus.PENDING
    assert outbox_row.attempts == 1
    assert outbox_row.last_error_class == "TransientPublishError"
    assert outbox_row.claim_token is None
    assert outbox_row.claim_expires_at is None
    # available_at должен быть смещен в будущее минимум на 5 секунд
    assert outbox_row.available_at >= outbox_row.created_at + timedelta(seconds=4.9)


async def test_relay_permanent_failure_quarantines_message_in_database(
    identity_client: TestClient,
    database_sessionmaker: async_sessionmaker[AsyncSession],
    database_session: AsyncSession,
) -> None:
    """Проверяет, что неисправимая ошибка переводит запись в QUARANTINED."""
    _, response = register(identity_client)
    user_id = response.json()["userId"]

    outbox_row = await database_session.scalar(
        select(OutboxMessageORM).where(OutboxMessageORM.key == user_id)
    )
    assert outbox_row is not None

    publisher = RecordingBrokerPublisher()
    publisher.fail_rules[outbox_row.id] = PermanentPublishError(
        "Message schema violation"
    )

    scope_factory = build_scope_factory(database_sessionmaker)
    relay = OutboxRelayService(publisher=publisher, scope_factory=scope_factory)

    processed = await relay.run_once(batch_size=10)
    assert processed == 1

    await database_session.refresh(outbox_row)
    assert outbox_row.status == OutboxStatus.QUARANTINED
    assert outbox_row.terminal_at is not None
    assert outbox_row.last_error_class == "PermanentPublishError"
    assert outbox_row.attempts == 1


async def test_concurrent_relays_process_batch_without_duplicates(
    identity_client: TestClient,
    database_sessionmaker: async_sessionmaker[AsyncSession],
    database_session: AsyncSession,
) -> None:
    """Проверяет конкурентную работу нескольких инстансов релея (FOR UPDATE SKIP LOCKED)."""
    # Регистрируем 10 пользователей
    for _ in range(10):
        register(identity_client)

    publisher1 = RecordingBrokerPublisher()
    publisher2 = RecordingBrokerPublisher()
    scope_factory = build_scope_factory(database_sessionmaker)

    relay1 = OutboxRelayService(publisher=publisher1, scope_factory=scope_factory)
    relay2 = OutboxRelayService(publisher=publisher2, scope_factory=scope_factory)

    # Запускаем два конкурирующих релея одновременно
    await asyncio.gather(
        relay1.run_once(batch_size=10),
        relay2.run_once(batch_size=10),
    )

    all_published_ids = [m.id for m in publisher1.published] + [
        m.id for m in publisher2.published
    ]
    # Все 10 сообщений должны быть обработаны
    assert len(all_published_ids) == 10
    # И при этом нет ни одного дубликата между воркерами
    assert len(set(all_published_ids)) == 10


async def test_relay_recovers_expired_claimed_lease(
    database_sessionmaker: async_sessionmaker[AsyncSession],
    database_session: AsyncSession,
) -> None:
    """Проверяет, что релей безопасно перехватывает 'зависшие' сообщения от упавшего воркера."""
    past = utc_now() - timedelta(minutes=5)
    dead_worker_token = uuid.uuid4()
    msg_id = uuid.uuid7()

    # Записываем зависшее сообщение в состоянии CLAIMED с протухшим lease
    zombie_message = OutboxMessageORM(
        id=msg_id,
        kind=OutboxKind.EVENT,
        topic="identity.user-registered.v1",
        key="user-zombie",
        type="identity.user_registered.v1",
        payload={"userId": "user-zombie"},
        status=OutboxStatus.CLAIMED,
        attempts=1,
        available_at=past,
        claim_token=dead_worker_token,
        claim_expires_at=past,  # Lease expired!
        created_at=past,
        redrive_count=0,
    )
    database_session.add(zombie_message)
    await database_session.commit()

    publisher = RecordingBrokerPublisher()
    scope_factory = build_scope_factory(database_sessionmaker)
    relay = OutboxRelayService(publisher=publisher, scope_factory=scope_factory)

    processed = await relay.run_once(batch_size=10)
    assert processed == 1
    assert len(publisher.published) == 1
    assert publisher.published[0].id == msg_id

    # Проверяем успешную финализацию в БД
    await database_session.refresh(zombie_message)
    assert zombie_message.status == OutboxStatus.SUCCESS
    assert zombie_message.terminal_at is not None
    assert zombie_message.claim_token is None
    assert zombie_message.attempts == 2
