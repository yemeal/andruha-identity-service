from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.dto.idempotency import (
    CompletedIdempotencyResult,
)
from app.application.value_objects.idempotency import IdempotencyIdentity
from app.domain.base import utc_now
from app.infrastructure.database.models.idempotency_records import IdempotencyRecordORM


class IdempotencyRecordRepository:
    def __init__(self, session: AsyncSession, *, retention_seconds: int) -> None:
        self._session = session
        self._retention_seconds = retention_seconds

    async def get_completed(
        self, identity: IdempotencyIdentity
    ) -> CompletedIdempotencyResult | None:
        result = await self._session.execute(
            select(IdempotencyRecordORM).where(
                IdempotencyRecordORM.subject_id == identity.subject_id,
                IdempotencyRecordORM.operation == identity.operation,
                IdempotencyRecordORM.key_hash == identity.key_hash,
            )
        )
        row = result.scalar_one_or_none()
        if row is None or row.expires_at <= utc_now():
            return None
        return CompletedIdempotencyResult(
            request_hash=row.request_hash,
            result_type=row.result_type,
            result_payload=row.result_payload,
            result_version=row.result_version,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            resource_version=row.resource_version,
        )

    async def try_add_completed(
        self, identity: IdempotencyIdentity, completed: CompletedIdempotencyResult
    ) -> bool:
        now = utc_now()
        statement = (
            pg_insert(IdempotencyRecordORM)
            .values(
                subject_id=identity.subject_id,
                operation=identity.operation,
                key_hash=identity.key_hash,
                request_hash=completed.request_hash,
                fingerprint_version=1,
                result_type=completed.result_type,
                result_payload=completed.result_payload,
                result_version=completed.result_version,
                resource_type=completed.resource_type,
                resource_id=str(completed.resource_id)
                if completed.resource_id is not None
                else None,
                resource_version=completed.resource_version,
                created_at=now,
                completed_at=now,
                expires_at=now + timedelta(seconds=self._retention_seconds),
            )
            .on_conflict_do_nothing(
                index_elements=["subject_id", "operation", "key_hash"]
            )
            .returning(IdempotencyRecordORM.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def delete_expired(self, *, expires_at: datetime, limit: int) -> int:
        if limit <= 0:
            raise ValueError("cleanup limit must be positive")
        candidates = (
            select(IdempotencyRecordORM.id)
            .where(IdempotencyRecordORM.expires_at <= expires_at)
            .order_by(IdempotencyRecordORM.expires_at, IdempotencyRecordORM.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("expired_idempotency_records")
        )
        result = await self._session.execute(
            delete(IdempotencyRecordORM)
            .where(IdempotencyRecordORM.id.in_(select(candidates.c.id)))
            .returning(IdempotencyRecordORM.id)
        )
        return len(result.scalars().all())
