from typing import Self

from pydantic import Field, model_validator

from app.core.settings.base import BaseContextSettings


class RegistrationSettings(BaseContextSettings):
    REGISTRATION_POLL_INTERVAL_SECONDS: float = Field(default=2.0, gt=0)
    REGISTRATION_BATCH_SIZE: int = Field(default=25, gt=0, le=500)
    REGISTRATION_CLAIM_LEASE_SECONDS: float = Field(default=30.0, gt=0)
    REGISTRATION_RETRY_INITIAL_SECONDS: float = Field(default=1.0, gt=0)
    REGISTRATION_RETRY_MAX_SECONDS: float = Field(default=60.0, gt=0)
    REGISTRATION_RETRY_EXPONENT: float = Field(default=2.0, ge=1.0)
    REGISTRATION_RETRY_JITTER_RATIO: float = Field(default=0.2, ge=0.0, le=1.0)
    REGISTRATION_RETRY_MAX_ATTEMPTS: int = Field(default=10, gt=0)
    REGISTRATION_SHUTDOWN_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def validate_retry_window(self) -> Self:
        if self.REGISTRATION_RETRY_MAX_SECONDS < (
            self.REGISTRATION_RETRY_INITIAL_SECONDS
        ):
            raise ValueError(
                "REGISTRATION_RETRY_MAX_SECONDS must be at least "
                "REGISTRATION_RETRY_INITIAL_SECONDS"
            )
        return self
