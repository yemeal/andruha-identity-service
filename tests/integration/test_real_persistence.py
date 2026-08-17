from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import Text, delete, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)

from app.application.ports.dto.idempotency import (
    CompletedIdempotencyResult,
    StoredResult,
)
from app.application.services.durable_idempotency import DurableExecutionService
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    BeginAction,
    ExecutionOutcome,
    IdempotencyIdentity,
)
from app.core.settings import get_settings
from app.domain.users import User
from app.entrypoints.http.main import create_app
from app.infrastructure.cache.valkey_idempotency_store import (
    ValkeyHotIdempotencyStore,
)
from app.infrastructure.database.models import (
    AuthSessionORM,
    Base,
    IdempotencyRecordORM,
    RefreshTokenORM,
    UserORM,
)
from app.infrastructure.database.repositories.idempotency_record_repository import (
    IdempotencyRecordRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW

pytestmark = pytest.mark.integration


def _database_url() -> str:
    return os.getenv(
        "IDENTITY_TEST_DATABASE_URL",
        "postgresql+asyncpg://andruha_identity:identity-local-only@identity-postgres:5432/andruha_identity",
    )


def _valkey_url() -> str:
    return os.getenv("IDENTITY_TEST_VALKEY_URL", "redis://valkey:6379/15")


async def test_alembic_schema_matches_final_orm_metadata() -> None:
    engine = create_async_engine(_database_url())

    def schema_diff(connection: AsyncConnection) -> list[object]:
        context = MigrationContext.configure(connection)
        return compare_metadata(context, Base.metadata)

    try:
        async with engine.connect() as connection:
            differences = await connection.run_sync(schema_diff)
        assert differences == []
        assert set(Base.metadata.tables) == {
            "users",
            "auth_sessions",
            "refresh_tokens",
            "idempotency_records",
        }
    finally:
        await engine.dispose()


async def test_real_valkey_lua_owner_cas_and_replay() -> None:
    client = Redis.from_url(_valkey_url(), decode_responses=False)
    namespace = f"identity-test:idempotency:{uuid.uuid4()}"
    store = ValkeyHotIdempotencyStore(
        client,
        result_ttl_seconds=300,
        key_namespace=namespace,
    )
    identity = IdempotencyIdentity(
        subject_id="public-refresh",
        operation="auth.refresh",
        key_hash=hash_idempotency_key("real-valkey-key"),
    )
    owner = uuid.uuid4()
    other_owner = uuid.uuid4()
    request_hash = compute_request_hash({"refresh_token_digest": "digest-only"})
    completed = CompletedIdempotencyResult(
        request_hash=request_hash,
        result_type="auth.refresh.success",
        result_payload={
            "version": 1,
            "algorithm": "AES-256-GCM",
            "key_id": "test",
            "nonce": "encrypted-nonce",
            "ciphertext": "encrypted-result",
        },
        resource_type="refresh_token",
        resource_id=uuid.uuid4(),
    )
    try:
        acquired = await store.begin(identity, request_hash, owner, 30)
        assert acquired.action is BeginAction.ACQUIRED
        assert await store.renew(identity, other_owner, 30) is False
        assert await store.complete(identity, other_owner, completed) is False
        assert await store.complete(identity, owner, completed) is True

        replay = await store.begin(identity, request_hash, uuid.uuid4(), 30)
        assert replay.action is BeginAction.REPLAY
        assert replay.completed is not None
        assert replay.completed.request_hash == completed.request_hash
        assert replay.completed.result_type == completed.result_type
        assert replay.completed.result_payload == completed.result_payload
        assert replay.completed.resource_type == completed.resource_type
        assert str(replay.completed.resource_id) == str(completed.resource_id)
    finally:
        keys = await client.keys(f"{namespace}:*")
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_real_postgres_business_effect_and_result_commit_atomically() -> None:
    engine = create_async_engine(_database_url())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = IdempotencyIdentity(
        subject_id="public-refresh",
        operation="auth.refresh.integration",
        key_hash=hash_idempotency_key(str(uuid.uuid4())),
    )
    request_hash = compute_request_hash({"refresh_token_digest": "integration"})
    email = f"identity-integration-{uuid.uuid4()}@example.com"
    created_user_id: uuid.UUID | None = None

    try:
        async with sessions() as session:
            users = UserRepository(session)
            records = IdempotencyRecordRepository(session, retention_seconds=300)
            durable = DurableExecutionService(records, SQLAlchemyAsyncUOW(session))

            async def effect() -> StoredResult:
                nonlocal created_user_id
                user = await users.create(User(email=email, password_hash="test-hash"))
                created_user_id = user.id
                return StoredResult(
                    result_type="auth.refresh.rejected",
                    result_payload={"code": "safe-test-result"},
                )

            result = await durable.execute_once(identity, request_hash, effect)
            assert result.outcome is ExecutionOutcome.EXECUTED

        assert created_user_id is not None
        async with sessions() as session:
            assert await session.get(UserORM, created_user_id) is not None
            record = await session.scalar(
                select(IdempotencyRecordORM).where(
                    IdempotencyRecordORM.subject_id == identity.subject_id,
                    IdempotencyRecordORM.operation == identity.operation,
                    IdempotencyRecordORM.key_hash == identity.key_hash,
                )
            )
            assert record is not None
            assert record.result_payload == {"code": "safe-test-result"}
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                delete(IdempotencyRecordORM).where(
                    IdempotencyRecordORM.subject_id == identity.subject_id,
                    IdempotencyRecordORM.operation == identity.operation,
                    IdempotencyRecordORM.key_hash == identity.key_hash,
                )
            )
            if created_user_id is not None:
                await session.execute(
                    delete(UserORM).where(UserORM.id == created_user_id)
                )
        await engine.dispose()


def test_refresh_canaries_never_enter_storage_logs_errors_or_metrics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "jwt-private.pem"
    public_path = tmp_path / "jwt-public.pem"
    replay_path = tmp_path / "replay.key"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    replay_path.write_bytes(os.urandom(32))
    monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(private_path))
    monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(public_path))
    monkeypatch.setenv("REPLAY_ENCRYPTION_KEY_PATHS", f"replay-v1={replay_path}")
    monkeypatch.setenv("DEV_LOGS", "false")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    get_settings.cache_clear()

    email = f"identity-canary-{uuid.uuid4()}@example.com"
    idempotency_key = f"canary-key-{uuid.uuid4()}"
    key_hash = hash_idempotency_key(idempotency_key)
    canaries: list[str] = []
    try:
        with TestClient(create_app()) as client:
            register = client.post(
                "/api/v1/auth/register",
                json={"email": email, "password": "Canary-password-123"},
            )
            assert register.status_code == 201
            login = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "Canary-password-123"},
            )
            assert login.status_code == 204
            canaries.extend(
                [
                    client.cookies["access_token"],
                    client.cookies["refresh_token"],
                ]
            )

            refreshed = client.post(
                "/api/v1/auth/refresh",
                headers={"Idempotency-Key": idempotency_key},
            )
            assert refreshed.status_code == 204
            canaries.extend(
                [
                    client.cookies["access_token"],
                    client.cookies["refresh_token"],
                ]
            )

            conflict = client.post(
                "/api/v1/auth/refresh",
                headers={"Idempotency-Key": idempotency_key},
            )
            assert conflict.status_code == 409
            metrics = client.get("/metrics")
            assert metrics.status_code == 200
            public_output = conflict.text + metrics.text
            assert all(secret not in public_output for secret in canaries)

        captured_logs = capfd.readouterr()
        combined_logs = captured_logs.out + captured_logs.err
        assert all(secret not in combined_logs for secret in canaries)

        async def inspect_and_cleanup() -> None:
            engine = create_async_engine(_database_url())
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            valkey = Redis.from_url(_valkey_url(), decode_responses=False)
            try:
                async with sessions() as session:
                    payloads = list(
                        await session.scalars(
                            select(
                                sa_cast(IdempotencyRecordORM.result_payload, Text)
                            ).where(IdempotencyRecordORM.key_hash == key_hash)
                        )
                    )
                    stored_payloads = "".join(payloads)
                    assert payloads
                    assert all(secret not in stored_payloads for secret in canaries)

                    user_id = await session.scalar(
                        select(UserORM.id).where(UserORM.email == email)
                    )
                    assert user_id is not None
                    session_ids = list(
                        await session.scalars(
                            select(AuthSessionORM.id).where(
                                AuthSessionORM.user_id == user_id
                            )
                        )
                    )
                    async with session.begin_nested():
                        await session.execute(
                            delete(IdempotencyRecordORM).where(
                                IdempotencyRecordORM.key_hash == key_hash
                            )
                        )
                        await session.execute(
                            delete(RefreshTokenORM).where(
                                RefreshTokenORM.session_id.in_(session_ids)
                            )
                        )
                        await session.execute(
                            delete(AuthSessionORM).where(
                                AuthSessionORM.id.in_(session_ids)
                            )
                        )
                        await session.execute(
                            delete(UserORM).where(UserORM.id == user_id)
                        )
                    await session.commit()

                keys = await valkey.keys("andruha-identity-service:idempotency:v1:*")
                values: list[bytes] = []
                for key in keys:
                    values.extend((await valkey.hgetall(key)).values())
                raw_hot_values = b"".join(values)
                assert all(secret.encode() not in raw_hot_values for secret in canaries)
                if keys:
                    await valkey.delete(*keys)
            finally:
                await valkey.aclose()
                await engine.dispose()

        import asyncio

        asyncio.run(inspect_and_cleanup())
    finally:
        get_settings.cache_clear()
