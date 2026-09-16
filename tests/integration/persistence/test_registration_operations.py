from datetime import UTC, datetime, timedelta
import hashlib
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.dto.registration import (
    RegistrationOperation,
    RegistrationStatus,
)
from app.infrastructure.database.repositories.registration_operation_repository import (
    RegistrationOperationRepository,
)

pytestmark = pytest.mark.integration


async def test_expired_registration_claim_is_reclaimed_and_fenced(
    database_session: AsyncSession,
) -> None:
    repository = RegistrationOperationRepository(database_session)
    claimed_at = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    stale_token = uuid.uuid4()
    operation = RegistrationOperation(
        email="fenced-registration@example.com",
        password_hash="argon2-hash",
        key_hash=hashlib.sha256(b"fenced-registration-key").digest(),
        status=RegistrationStatus.CLAIMED,
        available_at=claimed_at,
        claim_token=stale_token,
        claim_expires_at=claimed_at + timedelta(seconds=1),
        created_at=claimed_at,
    )
    assert await repository.try_create(operation) == operation
    await database_session.commit()

    reclaimed_at = claimed_at + timedelta(seconds=2)
    current_token = uuid.uuid4()
    reclaimed = await repository.claim_by_key_hash(
        operation.key_hash,
        owner_token=current_token,
        claimed_at=reclaimed_at,
        claim_expires_at=reclaimed_at + timedelta(seconds=30),
    )
    assert reclaimed is not None
    assert reclaimed.claim_token == current_token
    await database_session.commit()

    assert not await repository.complete(
        operation.id,
        owner_token=stale_token,
        completed_at=reclaimed_at + timedelta(seconds=1),
    )
    assert await repository.complete(
        operation.id,
        owner_token=current_token,
        completed_at=reclaimed_at + timedelta(seconds=1),
    )
    await database_session.commit()

    completed = await repository.get_by_key_hash(operation.key_hash)
    assert completed is not None
    assert completed.status is RegistrationStatus.COMPLETED
    assert completed.password_hash is None
