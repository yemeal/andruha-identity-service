import uuid
from datetime import datetime

from sqlalchemy import UUID, CheckConstraint, DateTime, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.application.ports.dto.outbox import OutboxKind, OutboxStatus
from app.infrastructure.database.models.base import Base, TimestampMixin, UuidMixin


class OutboxMessageORM(Base, UuidMixin, TimestampMixin):
    __tablename__ = "outbox"

    kind: Mapped[OutboxKind] = mapped_column(SAEnum(OutboxKind))
    topic: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(SAEnum(OutboxStatus))
    attempts: Mapped[int]
    last_error_class: Mapped[str | None] = mapped_column(String(255))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redrive_count: Mapped[int]

    __table_args__ = (
        CheckConstraint(
            "("
            "status = 'PENDING' "
            "AND claim_token IS NULL "
            "AND claim_expires_at IS NULL "
            "AND terminal_at IS NULL"
            ") OR ("
            "status = 'CLAIMED' "
            "AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL "
            "AND terminal_at IS NULL"
            ") OR ("
            "status IN ('SUCCESS', 'QUARANTINED') "
            "AND claim_token IS NULL "
            "AND claim_expires_at IS NULL "
            "AND terminal_at IS NOT NULL"
            ")",
            name="ck_outbox_lifecycle",
        ),
        CheckConstraint(
            "attempts >= 0 AND redrive_count >= 0",
            name="ck_outbox_counters_nonnegative",
        ),
        # для быстрой выборки пачки релеем
        Index(
            "ix_outbox_dispatch",
            "status",
            "available_at",
            "created_at",
            "id",
        ),
        # для подбора зависших воркеров
        Index(
            "ix_outbox_claim_recovery",
            "status",
            "claim_expires_at",
            "id",
        ),
        # для сохранения порядка сообщений по одному key
        Index(
            "ix_outbox_key_order",
            "key",
            "created_at",
            "id",
            "status",
        ),
        # для очистки старых записей
        Index(
            "ix_outbox_status_terminal_at",
            "status",
            "terminal_at",
            "id",
        ),
    )
