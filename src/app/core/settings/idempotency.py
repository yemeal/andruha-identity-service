from pydantic import Field

from app.core.settings.base import BaseContextSettings


class IdempotencySettings(BaseContextSettings):
    IDEMPOTENCY_LEASE_SECONDS: int = Field(default=30, gt=0)
    IDEMPOTENCY_RESULT_TTL_SECONDS: int = Field(default=300, gt=0)
    IDEMPOTENCY_CB_FAILURES: int = Field(default=3, gt=0)
    IDEMPOTENCY_CB_RECOVERY_SECONDS: float = Field(default=10.0, gt=0)
