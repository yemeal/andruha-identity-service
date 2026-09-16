from datetime import datetime
from typing import Protocol
from uuid import UUID


class ProfileProvisionerProtocol(Protocol):
    """Confirm that the user's default profile and settings have been committed."""

    async def create_profile(self, user_id: UUID, registered_at: datetime) -> None: ...
