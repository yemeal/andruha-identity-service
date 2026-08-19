from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.models.base import Base, CreatedAtMixin, UuidMixin


class RefreshTokenORM(Base, UuidMixin, CreatedAtMixin):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        CheckConstraint(
            "octet_length(token_hash) = 32",
            name="ck_refresh_tokens_token_hash_length",
        ),
        CheckConstraint(
            "used_at IS NULL OR used_at >= created_at",
            name="ck_refresh_tokens_used_after_created",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    # хранится хэш, не сам токен: утечка таблицы не даёт рабочих refresh-токенов
    token_hash: Mapped[bytes] = mapped_column(
        LargeBinary(32),
        unique=True,
        index=True,
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
