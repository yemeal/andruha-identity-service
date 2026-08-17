from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class IdempotencyIdentity(BaseModel):
    """Immutable identity of one idempotent application operation."""

    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1, max_length=255)
    operation: str = Field(min_length=1, max_length=100)
    key_hash: bytes = Field(min_length=32, max_length=32)


class BeginAction(Enum):
    ACQUIRED = "ACQUIRED"
    REPLAY = "REPLAY"
    CONFLICT = "CONFLICT"
    IN_PROGRESS = "IN_PROGRESS"


class ExecutionOutcome(Enum):
    EXECUTED = "EXECUTED"
    REPLAY = "REPLAY"
    CONFLICT = "CONFLICT"
    IN_PROGRESS = "IN_PROGRESS"
