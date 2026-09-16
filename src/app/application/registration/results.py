from datetime import datetime
from math import ceil

from app.application.ports.dto.registration import (
    RegistrationOperation,
    RegistrationOutcome,
    RegistrationResult,
    RegistrationStatus,
)


def pending_result(
    operation: RegistrationOperation,
    *,
    now: datetime,
    retry_at: datetime | None = None,
) -> RegistrationResult:
    effective_retry_at = retry_at or operation.available_at
    if (
        retry_at is None
        and operation.status is RegistrationStatus.CLAIMED
        and operation.claim_expires_at is not None
    ):
        effective_retry_at = operation.claim_expires_at
    retry_after_seconds = max(
        1,
        ceil((effective_retry_at - now).total_seconds()),
    )
    return RegistrationResult(
        operation_id=operation.id,
        user_id=operation.user_id,
        outcome=RegistrationOutcome.PENDING,
        retry_after_seconds=retry_after_seconds,
    )
