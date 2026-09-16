from datetime import datetime
import uuid

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.ports.dto.outbox import OutboxKind, OutboxMessage, OutboxStatus
from app.application.ports.events.base import IntegrationEventProtocol
from app.application.ports.repositories.outbox import OutboxRepositoryProtocol
from app.infrastructure.database.models import OutboxMessageORM
from app.infrastructure.database.repositories.base_repository import (
    SQLAlchemyAsyncRepository,
)


class OutboxRepository(
    SQLAlchemyAsyncRepository[OutboxMessage, OutboxMessageORM, uuid.UUID],
    OutboxRepositoryProtocol,
):
    def __init__(self, session: AsyncSession, topic: str, producer: str) -> None:
        super().__init__(session, OutboxMessage, OutboxMessageORM)
        self._topic = topic
        self._producer = producer

    async def publish(self, event: IntegrationEventProtocol) -> None:
        orm_model = OutboxMessageORM(
            id=event.event_id,
            kind=OutboxKind.EVENT,
            topic=self._topic,
            key=event.partition_key,
            type=event.event_type,
            payload=event.to_envelope_dict(producer=self._producer),
            status=OutboxStatus.PENDING,
            attempts=0,
            available_at=event.occurred_at,
            created_at=event.occurred_at,
            redrive_count=0,
        )
        self._session.add(orm_model)

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[OutboxMessage]:
        """Claim eligible messages without overtaking unfinished messages of the same key."""
        if limit <= 0:
            raise ValueError("claim limit must be positive")
        if claim_expires_at <= claimed_at:
            raise ValueError("claim expiration must be after claim time")

        candidate = aliased(OutboxMessageORM, name="candidate_outbox")
        earlier = aliased(OutboxMessageORM, name="earlier_outbox")

        # UUID breaks ties when creation timestamps match.
        earlier_position = or_(
            earlier.created_at < candidate.created_at,
            and_(
                earlier.created_at == candidate.created_at,
                earlier.id < candidate.id,
            ),
        )
        # An unfinished predecessor blocks later messages with the same key.
        has_earlier_nonterminal_for_key = exists(
            select(earlier.id).where(
                earlier.key == candidate.key,
                earlier.status.in_(
                    (OutboxStatus.PENDING, OutboxStatus.CLAIMED),
                ),
                earlier_position,
            )
        )
        # An expired lease allows another worker to recover the message.
        eligible = or_(
            and_(
                candidate.status == OutboxStatus.PENDING,
                candidate.available_at <= claimed_at,
            ),
            and_(
                candidate.status == OutboxStatus.CLAIMED,
                candidate.claim_expires_at <= claimed_at,
            ),
        )

        claimable_ids = (
            select(candidate.id)
            .where(
                eligible,
                ~has_earlier_nonterminal_for_key,
            )
            .order_by(candidate.created_at.asc(), candidate.id.asc())
            .limit(limit)
            .with_for_update(of=candidate, skip_locked=True)
            .cte("claimable_outbox")
        )

        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id.in_(select(claimable_ids.columns.id)),
            )
            .values(
                status=OutboxStatus.CLAIMED,
                claim_token=owner_token,
                claim_expires_at=claim_expires_at,
                terminal_at=None,
            )
            .returning(OutboxMessageORM)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        result = await self._session.execute(statement)
        rows = list(result.scalars().all())
        # Detach bulk-updated rows before later transaction scopes reuse the session.
        self._session.expunge_all()
        rows.sort(key=lambda row: (row.created_at, row.id))
        return [self._to_domain(row) for row in rows]

    async def finalize_published(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        published_at: datetime,
    ) -> bool:
        """Mark delivery complete only while the worker still owns the claim."""
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.CLAIMED,
                OutboxMessageORM.claim_token == owner_token,
            )
            .values(
                status=OutboxStatus.SUCCESS,
                attempts=OutboxMessageORM.attempts + 1,
                last_error_class=None,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=published_at,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def schedule_retry(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        available_at: datetime,
        error_class: str = "TransientPublishError",
    ) -> bool:
        """Release the claim and defer the next attempt until available_at."""
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.CLAIMED,
                OutboxMessageORM.claim_token == owner_token,
            )
            .values(
                status=OutboxStatus.PENDING,
                attempts=OutboxMessageORM.attempts + 1,
                last_error_class=error_class,
                available_at=available_at,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=None,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def quarantine(
        self,
        message_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        quarantined_at: datetime,
        error_class: str = "PermanentOutboxPublishError",
    ) -> bool:
        """Quarantine a failed message and unblock later messages of the same key."""
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.CLAIMED,
                OutboxMessageORM.claim_token == owner_token,
            )
            .values(
                status=OutboxStatus.QUARANTINED,
                attempts=OutboxMessageORM.attempts + 1,
                last_error_class=error_class,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=quarantined_at,
            )
            .returning(OutboxMessageORM.id)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def redrive_quarantined(
        self,
        message_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool:
        """Redrive only when no later message of the same key exists."""
        target = aliased(OutboxMessageORM, name="redrive_target")
        later = aliased(OutboxMessageORM, name="later_outbox")
        later_exists = exists(
            select(later.id)
            .select_from(target)
            .join(
                later,
                and_(
                    later.key == target.key,
                    or_(
                        later.created_at > target.created_at,
                        and_(
                            later.created_at == target.created_at,
                            later.id > target.id,
                        ),
                    ),
                ),
            )
            .where(target.id == message_id)
        )
        statement = (
            update(OutboxMessageORM)
            .where(
                OutboxMessageORM.id == message_id,
                OutboxMessageORM.status == OutboxStatus.QUARANTINED,
                ~later_exists,
            )
            .values(
                status=OutboxStatus.PENDING,
                attempts=0,
                last_error_class=None,
                available_at=available_at,
                claim_token=None,
                claim_expires_at=None,
                terminal_at=None,
                redrive_count=OutboxMessageORM.redrive_count + 1,
            )
            .returning(OutboxMessageORM)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def delete_terminal_before(
        self,
        *,
        terminal_at: datetime,
        limit: int,
    ) -> int:
        """Delete a bounded batch of terminal messages older than the cutoff."""
        if limit <= 0:
            raise ValueError("cleanup limit must be positive")
        candidates = (
            select(OutboxMessageORM.id)
            .where(
                OutboxMessageORM.status.in_(
                    (OutboxStatus.SUCCESS, OutboxStatus.QUARANTINED),
                ),
                OutboxMessageORM.terminal_at < terminal_at,
            )
            .order_by(
                OutboxMessageORM.terminal_at.asc(),
                OutboxMessageORM.created_at.asc(),
                OutboxMessageORM.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("expired_terminal_outbox")
        )
        statement = (
            delete(OutboxMessageORM)
            .where(
                OutboxMessageORM.id.in_(
                    select(candidates.c.id),
                )
            )
            .returning(OutboxMessageORM.id)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())
