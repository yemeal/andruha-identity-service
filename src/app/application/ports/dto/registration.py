from datetime import datetime
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.base import utc_now
from app.domain.value_objects.email import NormalizedEmail


class RegistrationStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class RegistrationOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"


class RegistrationOperation(BaseModel):
    """Durable state of the cross-context registration process."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid7)
    user_id: uuid.UUID = Field(default_factory=uuid.uuid7)
    email: NormalizedEmail
    password_hash: str | None = Field(repr=False)
    key_hash: bytes = Field(min_length=32, max_length=32, repr=False)
    status: RegistrationStatus = RegistrationStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now)
    claim_token: uuid.UUID | None = None
    claim_expires_at: datetime | None = None
    last_error_class: str | None = None
    terminal_at: datetime | None = None
    redrive_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RegistrationOperation:
        timestamps = (
            self.available_at,
            self.claim_expires_at,
            self.terminal_at,
            self.created_at,
            self.updated_at,
        )
        if any(value is not None and value.utcoffset() is None for value in timestamps):
            raise ValueError("registration timestamps must be timezone-aware")

        has_token = self.claim_token is not None
        has_expiration = self.claim_expires_at is not None
        if has_token != has_expiration:
            raise ValueError("claim token and expiration must be set together")

        if self.status is RegistrationStatus.CLAIMED:
            if not has_token or self.terminal_at is not None:
                raise ValueError("CLAIMED registration requires a live claim")
        elif has_token:
            raise ValueError("only CLAIMED registration may hold a claim")

        if self.status in (RegistrationStatus.COMPLETED, RegistrationStatus.BLOCKED):
            if self.terminal_at is None:
                raise ValueError("terminal registration requires terminal_at")
        elif self.terminal_at is not None:
            raise ValueError("nonterminal registration cannot have terminal_at")

        if self.status is RegistrationStatus.COMPLETED:
            if self.password_hash is not None:
                raise ValueError("completed registration must scrub password_hash")
        elif self.password_hash is None:
            raise ValueError("non-completed registration requires password_hash")
        return self


class RegistrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: uuid.UUID
    user_id: uuid.UUID
    outcome: RegistrationOutcome
    retry_after_seconds: int | None = Field(default=None, ge=1)
