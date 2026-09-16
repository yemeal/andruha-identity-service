from collections.abc import Callable
from datetime import datetime

import structlog

from app.application.ports.repositories import AuthSessionRepositoryProtocol
from app.application.ports.security import OpaqueRefreshTokenCodecProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.use_cases.logout.command import LogoutCommand
from app.domain.base import utc_now

logger = structlog.get_logger(__name__)


class LogoutHandler:
    def __init__(
        self,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        uow: AsyncUOWProtocol,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = sessions
        self._codec = codec
        self._uow = uow
        self._clock = clock

    async def execute(self, command: LogoutCommand) -> None:
        if command.refresh_token is None:
            return
        async with self._uow:
            session = await self._sessions.get_by_refresh_hash_for_update(
                self._codec.digest(command.refresh_token)
            )
            if session is None:
                return
            session.revoke(self._clock())
            await self._sessions.save(session)
        logger.info("logout succeeded")
