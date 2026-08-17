from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.models import (
    AuthSessionORM,
    IdempotencyRecordORM,
    RefreshTokenORM,
    UserORM,
)

pytestmark = pytest.mark.integration


async def _create_user_and_session(
    session: AsyncSession,
    *,
    email: str,
) -> tuple[UserORM, AuthSessionORM]:
    user = UserORM(email=email, password_hash="$argon2id$integration")
    session.add(user)
    await session.flush()
    auth_session = AuthSessionORM(
        user_id=user.id,
        idle_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(auth_session)
    await session.flush()
    return user, auth_session


async def test_unique_email_is_enforced_by_postgres(
    database_session: AsyncSession,
) -> None:
    database_session.add_all(
        [
            UserORM(email="unique@example.com", password_hash="hash-a"),
            UserORM(email="unique@example.com", password_hash="hash-b"),
        ]
    )

    with pytest.raises(IntegrityError):
        await database_session.commit()
    await database_session.rollback()

    assert await database_session.scalar(select(func.count()).select_from(UserORM)) == 0


@pytest.mark.parametrize(
    "values",
    [
        {"email": None, "password_hash": "hash"},
        {"email": "missing-hash@example.com", "password_hash": None},
    ],
)
async def test_required_user_columns_reject_null(
    database_session: AsyncSession,
    values: dict[str, object],
) -> None:
    with pytest.raises(IntegrityError):
        await database_session.execute(insert(UserORM).values(**values))
        await database_session.commit()
    await database_session.rollback()


async def test_refresh_token_rejects_invalid_fk_and_non_sha256_digest(
    database_session: AsyncSession,
) -> None:
    database_session.add(RefreshTokenORM(session_id=uuid4(), token_hash=os.urandom(31)))

    with pytest.raises(IntegrityError):
        await database_session.commit()
    await database_session.rollback()


async def test_duplicate_refresh_digest_is_rejected(
    database_session: AsyncSession,
) -> None:
    _user, first_session = await _create_user_and_session(
        database_session,
        email="token-owner@example.com",
    )
    second_session = AuthSessionORM(
        user_id=first_session.user_id,
        idle_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    database_session.add(second_session)
    await database_session.flush()
    digest = os.urandom(32)
    database_session.add_all(
        [
            RefreshTokenORM(session_id=first_session.id, token_hash=digest),
            RefreshTokenORM(session_id=second_session.id, token_hash=digest),
        ]
    )

    with pytest.raises(IntegrityError):
        await database_session.commit()
    await database_session.rollback()


async def test_idempotency_identity_is_a_unique_durable_fence(
    database_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    common = {
        "subject_id": "public-refresh",
        "operation": "auth.refresh",
        "key_hash": os.urandom(32),
        "request_hash": os.urandom(32),
        "result_type": "auth.refresh.rejected",
        "result_payload": {"code": "safe"},
        "expires_at": now + timedelta(minutes=5),
    }
    database_session.add_all(
        [IdempotencyRecordORM(**common), IdempotencyRecordORM(**common)]
    )

    with pytest.raises(IntegrityError):
        await database_session.commit()
    await database_session.rollback()


@pytest.mark.failure_path
async def test_mid_transaction_constraint_failure_rolls_back_every_write(
    database_session: AsyncSession,
) -> None:
    email = "rollback@example.com"

    with pytest.raises(IntegrityError):
        async with database_session.begin():
            _user, auth_session = await _create_user_and_session(
                database_session,
                email=email,
            )
            database_session.add(
                RefreshTokenORM(
                    session_id=auth_session.id,
                    token_hash=os.urandom(31),
                )
            )
            await database_session.flush()

    assert (
        await database_session.scalar(
            select(func.count()).select_from(UserORM).where(UserORM.email == email)
        )
        == 0
    )
    assert (
        await database_session.scalar(select(func.count()).select_from(AuthSessionORM))
        == 0
    )
