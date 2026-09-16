from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.aggregates.user import UserRole
from app.domain.value_objects.email import NormalizedEmail


class CurrentUser(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: UUID
    email: NormalizedEmail
    role: UserRole
    created_at: datetime
