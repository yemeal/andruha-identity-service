from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.integration.conftest import IdentityInfrastructure

from app.application.ports.dto.idempotency import StoredResult
from app.application.services.durable_idempotency import DurableExecutionService
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    ExecutionOutcome,
    IdempotencyIdentity,
)
from app.domain.users import User
from app.infrastructure.database.models import IdempotencyRecordORM, UserORM
from app.infrastructure.database.repositories.idempotency_record_repository import (
    IdempotencyRecordRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW

pytestmark = pytest.mark.integration


def _identity(key: str) -> IdempotencyIdentity:
    return IdempotencyIdentity(
        subject_id="public-refresh",
        operation="auth.refresh.integration",
        key_hash=hash_idempotency_key(key),
    )


@pytest.mark.race
async def test_concurrent_durable_execution_has_one_committed_winner(
    clean_identity_state: IdentityInfrastructure,
) -> None:
    engine = create_async_engine(clean_identity_state.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = _identity("durable-race")
    request_hash = compute_request_hash({"payload": "same"})

    async def attempt(index: int) -> ExecutionOutcome:
        async with sessions() as session:
            durable = DurableExecutionService(
                IdempotencyRecordRepository(session, retention_seconds=30),
                SQLAlchemyAsyncUOW(session),
            )
            users = UserRepository(session)

            async def effect() -> StoredResult:
                await users.create(
                    User(
                        email=f"durable-winner-{index}@example.com",
                        password_hash="integration-hash",
                    )
                )
                return StoredResult(
                    result_type="integration.success",
                    result_payload={"winner": "committed"},
                )

            return (await durable.execute_once(identity, request_hash, effect)).outcome

    try:
        outcomes = await asyncio.gather(*(attempt(index) for index in range(32)))
        async with sessions() as session:
            user_count = await session.scalar(select(func.count()).select_from(UserORM))
            record_count = await session.scalar(
                select(func.count()).select_from(IdempotencyRecordORM)
            )
    finally:
        await engine.dispose()

    assert outcomes.count(ExecutionOutcome.EXECUTED) == 1
    assert outcomes.count(ExecutionOutcome.REPLAY) == 31
    assert user_count == 1
    assert record_count == 1


async def test_same_durable_key_with_different_payload_is_conflict_without_effect(
    clean_identity_state: IdentityInfrastructure,
) -> None:
    engine = create_async_engine(clean_identity_state.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = _identity("durable-conflict")
    effects = 0

    async def execute(request_hash: bytes) -> ExecutionOutcome:
        nonlocal effects
        async with sessions() as session:
            durable = DurableExecutionService(
                IdempotencyRecordRepository(session, retention_seconds=30),
                SQLAlchemyAsyncUOW(session),
            )

            async def effect() -> StoredResult:
                nonlocal effects
                effects += 1
                return StoredResult(
                    result_type="integration.success",
                    result_payload={"ok": True},
                )

            return (await durable.execute_once(identity, request_hash, effect)).outcome

    try:
        first = await execute(compute_request_hash({"payload": "first"}))
        second = await execute(compute_request_hash({"payload": "second"}))
    finally:
        await engine.dispose()

    assert first is ExecutionOutcome.EXECUTED
    assert second is ExecutionOutcome.CONFLICT
    assert effects == 1


@pytest.mark.failure_path
async def test_commit_failure_rolls_back_business_write_and_result(
    clean_identity_state: IdentityInfrastructure,
) -> None:
    engine = create_async_engine(clean_identity_state.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = _identity("commit-failure")
    request_hash = compute_request_hash({"payload": "commit-failure"})
    try:
        async with sessions() as session, session.begin():
            session.add(UserORM(email="duplicate@example.com", password_hash="first"))

        async with sessions() as session:
            durable = DurableExecutionService(
                IdempotencyRecordRepository(session, retention_seconds=30),
                SQLAlchemyAsyncUOW(session),
            )

            async def effect() -> StoredResult:
                session.add(
                    UserORM(email="duplicate@example.com", password_hash="second")
                )
                return StoredResult(
                    result_type="integration.success",
                    result_payload={"must": "rollback"},
                )

            with pytest.raises(IntegrityError):
                await durable.execute_once(identity, request_hash, effect)

        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(UserORM)
                    .where(UserORM.email == "duplicate@example.com")
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(IdempotencyRecordORM)
                )
                == 0
            )
    finally:
        await engine.dispose()


async def test_expired_durable_result_is_boundedly_cleaned_before_key_reuse(
    clean_identity_state: IdentityInfrastructure,
) -> None:
    engine = create_async_engine(clean_identity_state.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = _identity("expired-result")
    request_hash = compute_request_hash({"payload": "expires"})
    try:
        async with sessions() as session:
            records = IdempotencyRecordRepository(session, retention_seconds=1)
            durable = DurableExecutionService(records, SQLAlchemyAsyncUOW(session))

            async def effect() -> StoredResult:
                return StoredResult(
                    result_type="integration.success",
                    result_payload={"expires": True},
                )

            assert (
                await durable.execute_once(identity, request_hash, effect)
            ).outcome is ExecutionOutcome.EXECUTED

        await asyncio.sleep(1.1)

        async with sessions() as session:
            records = IdempotencyRecordRepository(session, retention_seconds=1)
            async with SQLAlchemyAsyncUOW(session):
                deleted = await records.delete_expired(
                    expires_at=datetime.now(UTC),
                    limit=10,
                )
        assert deleted == 1
    finally:
        await engine.dispose()
