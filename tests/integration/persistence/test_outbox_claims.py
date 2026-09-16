from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.dto.outbox import OutboxKind, OutboxStatus
from app.infrastructure.database.models.outbox import OutboxMessageORM
from app.infrastructure.database.repositories.outbox_repository import OutboxRepository

pytestmark = pytest.mark.integration


async def test_outbox_reclaim_refreshes_cached_owner(
    database_session: AsyncSession,
) -> None:
    now = datetime(2026, 9, 15, 10, tzinfo=UTC)
    stale_owner, current_owner = uuid.uuid4(), uuid.uuid4()
    row = OutboxMessageORM(
        id=uuid.uuid4(),
        kind=OutboxKind.EVENT,
        topic="identity.events",
        key="test-user",
        type="identity.user_registered.v1",
        payload={},
        status=OutboxStatus.CLAIMED,
        attempts=0,
        redrive_count=0,
        available_at=now,
        created_at=now,
        claim_token=stale_owner,
        claim_expires_at=now + timedelta(seconds=1),
    )
    database_session.add(row)
    await database_session.commit()
    repository = OutboxRepository(
        database_session, topic="identity.events", producer="identity"
    )

    messages = await repository.claim_batch(
        owner_token=current_owner,
        claimed_at=now + timedelta(seconds=2),
        claim_expires_at=now + timedelta(seconds=30),
        limit=1,
    )

    assert row.claim_token == current_owner
    assert len(messages) == 1
    assert messages[0].claim_token == current_owner
    assert not await repository.finalize_published(
        row.id, owner_token=stale_owner, published_at=now + timedelta(seconds=3)
    )
    assert await repository.finalize_published(
        row.id, owner_token=current_owner, published_at=now + timedelta(seconds=3)
    )


async def test_quarantined_outbox_redrive_updates_loaded_state(
    database_session: AsyncSession,
) -> None:
    now = datetime(2026, 9, 15, 10, tzinfo=UTC)
    row = OutboxMessageORM(
        id=uuid.uuid4(),
        kind=OutboxKind.EVENT,
        topic="identity.events",
        key="quarantined-user",
        type="identity.user_registered.v1",
        payload={},
        status=OutboxStatus.QUARANTINED,
        attempts=3,
        redrive_count=0,
        available_at=now,
        created_at=now,
        terminal_at=now,
        last_error_class="TransientPublishError",
    )
    database_session.add(row)
    await database_session.commit()
    repository = OutboxRepository(
        database_session, topic="identity.events", producer="identity"
    )

    assert await repository.redrive_quarantined(row.id, available_at=now)
    restored = await repository.get(row.id)
    assert restored is not None
    assert restored.status is OutboxStatus.PENDING
    assert restored.attempts == 0
    assert restored.redrive_count == 1
    assert restored.terminal_at is None
    assert restored.last_error_class is None
