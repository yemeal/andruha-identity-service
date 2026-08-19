from pydantic import Field, field_validator

from app.core.settings.base import BaseContextSettings


class OutboxSettings(BaseContextSettings):
    OUTBOX_POLL_INTERVAL_SECONDS: float = Field(default=2.0, gt=0.0)
    OUTBOX_BATCH_SIZE: int = Field(default=50, gt=0)
    OUTBOX_CLAIM_LEASE_SECONDS: float = Field(default=30.0, gt=0.0)
    OUTBOX_RETRY_INITIAL_SECONDS: float = Field(default=1.0, gt=0.0)
    OUTBOX_RETRY_MAX_SECONDS: float = Field(default=60.0, gt=0.0)
    OUTBOX_RETRY_EXPONENT: float = Field(default=2.0, ge=1.0)
    OUTBOX_RETRY_JITTER_RATIO: float = Field(default=0.2, ge=0.0, le=1.0)
    OUTBOX_RETRY_MAX_ATTEMPTS: int = Field(default=10, gt=0)
    OUTBOX_SHUTDOWN_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0.0)

    @field_validator("OUTBOX_RETRY_MAX_SECONDS")
    @classmethod
    def validate_max_retry_seconds(cls, v: float) -> float:
        return v
