from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.base import Base, CreatedAtMixin, UuidMixin


class AuthSessionORM(Base, UuidMixin, CreatedAtMixin):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint(
            "idle_expires_at > created_at",
            name="ck_auth_sessions_idle_expires_after_created",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_auth_sessions_revoked_after_created",
        ),
        Index(
            "ix_auth_sessions_idle_expires_at_active",
            "idle_expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_auth_sessions_revoked_at_not_null",
            "revoked_at",
            postgresql_where=text("revoked_at IS NOT NULL"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
