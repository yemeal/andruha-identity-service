import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.base import Base


class IdempotencyRecordORM(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "subject_id",
            "operation",
            "key_hash",
            name="uq_idempotency_records_identity",
        ),
        CheckConstraint(
            "octet_length(key_hash) = 32", name="ck_idempotency_records_key_hash_sha256"
        ),
        CheckConstraint(
            "octet_length(request_hash) = 32",
            name="ck_idempotency_records_request_hash_sha256",
        ),
        CheckConstraint(
            "fingerprint_version > 0", name="ck_idempotency_records_fingerprint_version"
        ),
        CheckConstraint(
            "result_version > 0", name="ck_idempotency_records_result_version"
        ),
        CheckConstraint(
            "(resource_type IS NULL AND resource_id IS NULL) OR (resource_type IS NOT NULL AND resource_id IS NOT NULL)",
            name="ck_idempotency_records_resource_pair",
        ),
        CheckConstraint(
            "resource_version IS NULL OR (resource_type IS NOT NULL AND resource_id IS NOT NULL AND resource_version > 0)",
            name="ck_idempotency_records_resource_version",
        ),
        CheckConstraint(
            "result_payload IS NOT NULL OR (resource_type IS NOT NULL AND resource_id IS NOT NULL)",
            name="ck_idempotency_records_replayable_result",
        ),
        CheckConstraint(
            "completed_at >= created_at", name="ck_idempotency_records_completion_time"
        ),
        CheckConstraint(
            "expires_at > completed_at", name="ck_idempotency_records_expiration"
        ),
        Index("ix_idempotency_records_expires_at", "expires_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    request_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    fingerprint_version: Mapped[int] = mapped_column(
        SmallInteger, default=1, server_default="1", nullable=False
    )
    result_type: Mapped[str] = mapped_column(String(100), nullable=False)
    result_payload: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    result_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    resource_type: Mapped[str | None] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(255))
    resource_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
