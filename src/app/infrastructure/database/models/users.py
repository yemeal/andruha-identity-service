from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.users import UserRole, UserStatus
from app.infrastructure.database.models.base import Base, TimestampMixin, UuidMixin


class UserORM(Base, UuidMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="userrole"),
        default=UserRole.USER,
        server_default=UserRole.USER.value,
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="userstatus"),
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
