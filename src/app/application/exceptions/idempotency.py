class IdempotencyError(Exception):
    """Base error for application-level idempotency orchestration."""


class IdempotencyStorageUnavailableError(IdempotencyError):
    """The optional hot store is unavailable; durable execution may continue."""


class IdempotencyKeyConflictError(IdempotencyError):
    pass


class IdempotencyRequestInProgressError(IdempotencyError):
    pass


class RefreshReplayUnavailableError(IdempotencyError):
    pass
