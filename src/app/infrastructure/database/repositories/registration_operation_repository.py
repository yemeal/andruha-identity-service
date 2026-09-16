from datetime import datetime
import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.application.ports.dto.registration import (
    RegistrationOperation,
    RegistrationStatus,
)
from app.application.ports.repositories.registration_operations import (
    RegistrationOperationRepositoryProtocol,
)
from app.domain.value_objects.email import NormalizedEmail
from app.infrastructure.database.models.registration_operations import (
    RegistrationOperationORM,
)
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)


class RegistrationOperationRepository(
    SQLAlchemyAsyncRepository[
        RegistrationOperation,
        RegistrationOperationORM,
        uuid.UUID,
    ],
    RegistrationOperationRepositoryProtocol,
):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RegistrationOperation, RegistrationOperationORM)

    async def try_create(
        self, operation: RegistrationOperation
    ) -> RegistrationOperation | None:
        statement = (
            pg_insert(RegistrationOperationORM)
            .values(**operation.model_dump())
            .on_conflict_do_nothing()
            .returning(RegistrationOperationORM)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return None if row is None else self._to_domain(row)

    async def get_by_key_hash(self, key_hash: bytes) -> RegistrationOperation | None:
        statement = select(RegistrationOperationORM).where(
            RegistrationOperationORM.key_hash == key_hash
        )
        row = await self._session.scalar(statement)
        return None if row is None else self._to_domain(row)

    async def get_by_email(
        self, email: NormalizedEmail
    ) -> RegistrationOperation | None:
        statement = select(RegistrationOperationORM).where(
            RegistrationOperationORM.email == email
        )
        row = await self._session.scalar(statement)
        return None if row is None else self._to_domain(row)

    async def claim_by_key_hash(
        self,
        key_hash: bytes,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> RegistrationOperation | None:
        return await self._claim(
            RegistrationOperationORM.key_hash == key_hash,
            owner_token=owner_token,
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
        )

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[RegistrationOperation]:
        if limit <= 0:
            raise ValueError("claim limit must be positive")
        self._validate_claim_window(claimed_at, claim_expires_at)
        eligible = self._eligible(claimed_at)
        claimable_ids = (
            select(RegistrationOperationORM.id)
            .where(eligible)
            .order_by(
                RegistrationOperationORM.available_at.asc(),
                RegistrationOperationORM.created_at.asc(),
                RegistrationOperationORM.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("claimable_registration_operations")
        )
        statement = (
            update(RegistrationOperationORM)
            .where(
                RegistrationOperationORM.id.in_(select(claimable_ids.c.id)),
            )
            .values(
                status=RegistrationStatus.CLAIMED,
                claim_token=owner_token,
                claim_expires_at=claim_expires_at,
                terminal_at=None,
            )
            .returning(RegistrationOperationORM)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        rows = list(result.scalars().all())
        self._session.expunge_all()
        rows.sort(key=lambda row: (row.available_at, row.created_at, row.id))
        return [self._to_domain(row) for row in rows]

    async def complete(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        completed_at: datetime,
    ) -> bool:
        statement = (
            update(RegistrationOperationORM)
            .where(
                RegistrationOperationORM.id == operation_id,
                RegistrationOperationORM.status == RegistrationStatus.CLAIMED,
                RegistrationOperationORM.claim_token == owner_token,
                RegistrationOperationORM.claim_expires_at > completed_at,
            )
            .values(
                status=RegistrationStatus.COMPLETED,
                attempts=RegistrationOperationORM.attempts + 1,
                password_hash=None,
                last_error_class=None,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=completed_at,
            )
            .returning(RegistrationOperationORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def schedule_retry(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        failed_at: datetime,
        available_at: datetime,
        error_class: str,
    ) -> bool:
        statement = (
            update(RegistrationOperationORM)
            .where(
                RegistrationOperationORM.id == operation_id,
                RegistrationOperationORM.status == RegistrationStatus.CLAIMED,
                RegistrationOperationORM.claim_token == owner_token,
                RegistrationOperationORM.claim_expires_at > failed_at,
            )
            .values(
                status=RegistrationStatus.PENDING,
                attempts=RegistrationOperationORM.attempts + 1,
                last_error_class=error_class,
                available_at=available_at,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=None,
            )
            .returning(RegistrationOperationORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def block(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        blocked_at: datetime,
        error_class: str,
    ) -> bool:
        statement = (
            update(RegistrationOperationORM)
            .where(
                RegistrationOperationORM.id == operation_id,
                RegistrationOperationORM.status == RegistrationStatus.CLAIMED,
                RegistrationOperationORM.claim_token == owner_token,
                RegistrationOperationORM.claim_expires_at > blocked_at,
            )
            .values(
                status=RegistrationStatus.BLOCKED,
                attempts=RegistrationOperationORM.attempts + 1,
                last_error_class=error_class,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=blocked_at,
            )
            .returning(RegistrationOperationORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def redrive_blocked(
        self,
        operation_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool:
        statement = (
            update(RegistrationOperationORM)
            .where(
                RegistrationOperationORM.id == operation_id,
                RegistrationOperationORM.status == RegistrationStatus.BLOCKED,
            )
            .values(
                status=RegistrationStatus.PENDING,
                attempts=0,
                last_error_class=None,
                available_at=available_at,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=None,
                redrive_count=RegistrationOperationORM.redrive_count + 1,
            )
            .returning(RegistrationOperationORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def _claim(
        self,
        predicate: ColumnElement[bool],
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> RegistrationOperation | None:
        self._validate_claim_window(claimed_at, claim_expires_at)
        statement = (
            update(RegistrationOperationORM)
            .where(predicate, self._eligible(claimed_at))
            .values(
                status=RegistrationStatus.CLAIMED,
                claim_token=owner_token,
                claim_expires_at=claim_expires_at,
                terminal_at=None,
            )
            .returning(RegistrationOperationORM)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        self._session.expunge(row)
        return self._to_domain(row)

    @staticmethod
    def _eligible(claimed_at: datetime) -> ColumnElement[bool]:
        return or_(
            and_(
                RegistrationOperationORM.status == RegistrationStatus.PENDING,
                RegistrationOperationORM.available_at <= claimed_at,
            ),
            and_(
                RegistrationOperationORM.status == RegistrationStatus.CLAIMED,
                RegistrationOperationORM.claim_expires_at <= claimed_at,
            ),
        )

    @staticmethod
    def _validate_claim_window(
        claimed_at: datetime, claim_expires_at: datetime
    ) -> None:
        if claim_expires_at <= claimed_at:
            raise ValueError("claim expiration must be after claim time")
