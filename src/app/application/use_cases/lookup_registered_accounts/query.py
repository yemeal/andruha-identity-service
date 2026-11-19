from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LookupRegisteredAccountsQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
