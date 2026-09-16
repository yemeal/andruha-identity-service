from datetime import datetime
import uuid

from sqlalchemy import (
    UUID,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.application.ports.dto.registration import RegistrationStatus
from app.infrastructure.database.models.base import Base, TimestampMixin, UuidMixin


class RegistrationOperationORM(Base, UuidMixin, TimestampMixin):
    __tablename__ = "registration_operations"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    status: Mapped[RegistrationStatus] = mapped_column(
        SAEnum(RegistrationStatus, name="registrationstatus"),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(255))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redrive_count: Mapped[int] = mapped_column(nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_registration_operations_user_id"),
        UniqueConstraint("email", name="uq_registration_operations_email"),
        UniqueConstraint("key_hash", name="uq_registration_operations_key_hash"),
        CheckConstraint(
            "octet_length(key_hash) = 32",
            name="ck_registration_operations_key_hash_sha256",
        ),
        CheckConstraint(
            "attempts >= 0 AND redrive_count >= 0",
            name="ck_registration_operations_counters_nonnegative",
        ),
        CheckConstraint(
            """(
                status = 'PENDING'
                AND password_hash IS NOT NULL
                AND claim_token IS NULL
                AND claim_expires_at IS NULL
                AND terminal_at IS NULL
            ) OR (
                status = 'CLAIMED'
                AND password_hash IS NOT NULL
                AND claim_token IS NOT NULL
                AND claim_expires_at IS NOT NULL
                AND terminal_at IS NULL
            ) OR (
                status = 'COMPLETED'
                AND password_hash IS NULL
                AND claim_token IS NULL
                AND claim_expires_at IS NULL
                AND terminal_at IS NOT NULL
            ) OR (
                status = 'BLOCKED'
                AND password_hash IS NOT NULL
                AND claim_token IS NULL
                AND claim_expires_at IS NULL
                AND terminal_at IS NOT NULL
            )""",
            name="ck_registration_operations_lifecycle",
        ),
        Index(
            "ix_registration_operations_dispatch",
            "status",
            "available_at",
            "created_at",
            "id",
        ),
        Index(
            "ix_registration_operations_claim_recovery",
            "status",
            "claim_expires_at",
            "id",
        ),
    )
