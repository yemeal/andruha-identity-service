from datetime import datetime
from uuid import UUID

from app.application.exceptions.profiles import ProfileProvisioningUnavailableError


class RecordingProfileProvisioner:
    def __init__(self) -> None:
        self.created: list[tuple[UUID, datetime]] = []
        self.unavailable = False

    async def create_profile(self, user_id: UUID, registered_at: datetime) -> None:
        if self.unavailable:
            raise ProfileProvisioningUnavailableError()
        self.created.append((user_id, registered_at))
