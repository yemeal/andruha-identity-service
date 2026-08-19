import dishka
from dishka import Provider, Scope
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.events import EventPublisherProtocol, UserRegisteredEvent
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    OutboxRepositoryProtocol,
    RefreshTokenRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.core.settings import AppSettings, KafkaSettings
from app.infrastructure.database.repositories.auth_session_repository import (
    AuthSessionRepository,
)
from app.infrastructure.database.repositories.outbox_repository import OutboxRepository
from app.infrastructure.database.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)
from app.infrastructure.database.repositories.user_repository import UserRepository


class RepositoriesProvider(Provider):
    scope = Scope.REQUEST

    @dishka.provide
    def users(self, session: AsyncSession) -> UserRepositoryProtocol:
        return UserRepository(session)

    @dishka.provide
    def tokens(self, session: AsyncSession) -> RefreshTokenRepositoryProtocol:
        return RefreshTokenRepository(session)

    @dishka.provide
    def sessions(self, session: AsyncSession) -> AuthSessionRepositoryProtocol:
        return AuthSessionRepository(session)

    @dishka.provide
    def outbox_repository(
        self,
        session: AsyncSession,
        kafka_settings: KafkaSettings,
        app_settings: AppSettings,
    ) -> OutboxRepositoryProtocol:
        return OutboxRepository(
            session=session,
            topic=kafka_settings.KAFKA_USER_REGISTERED_TOPIC,
            producer=app_settings.SERVICE_NAME,
        )

    @dishka.provide
    def user_registered_event_publisher(
        self,
        session: AsyncSession,
        kafka_settings: KafkaSettings,
        app_settings: AppSettings,
    ) -> EventPublisherProtocol[UserRegisteredEvent]:
        return OutboxRepository(
            session=session,
            topic=kafka_settings.KAFKA_USER_REGISTERED_TOPIC,
            producer=app_settings.SERVICE_NAME,
        )
