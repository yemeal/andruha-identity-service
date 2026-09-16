from datetime import datetime
from typing import Protocol
import uuid

from app.application.ports.dto.registration import RegistrationOperation
from app.domain.value_objects.email import NormalizedEmail


class RegistrationOperationRepositoryProtocol(Protocol):
    async def try_create(
        self, operation: RegistrationOperation
    ) -> RegistrationOperation | None: ...

    async def get_by_key_hash(
        self, key_hash: bytes
    ) -> RegistrationOperation | None: ...

    async def get_by_email(
        self, email: NormalizedEmail
    ) -> RegistrationOperation | None: ...

    async def claim_by_key_hash(
        self,
        key_hash: bytes,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> RegistrationOperation | None: ...

    async def claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        limit: int,
    ) -> list[RegistrationOperation]: ...

    async def complete(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        completed_at: datetime,
    ) -> bool: ...

    async def schedule_retry(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        failed_at: datetime,
        available_at: datetime,
        error_class: str,
    ) -> bool: ...

    async def block(
        self,
        operation_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        blocked_at: datetime,
        error_class: str,
    ) -> bool: ...

    async def redrive_blocked(
        self,
        operation_id: uuid.UUID,
        *,
        available_at: datetime,
    ) -> bool: ...
