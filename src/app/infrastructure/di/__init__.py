from dishka import AsyncContainer, make_async_container

from app.infrastructure.di.database import (
    DatabaseAppProvider,
    DatabaseRequestProvider,
)
from app.infrastructure.di.idempotency import (
    IdempotencyAppProvider,
    IdempotencyRequestProvider,
)
from app.infrastructure.di.messaging import MessagingProvider
from app.infrastructure.di.repositories import RepositoriesProvider
from app.infrastructure.di.security import SecurityProvider
from app.infrastructure.di.services import ServicesProvider
from app.infrastructure.di.settings import SettingsProvider


def create_container() -> AsyncContainer:
    return make_async_container(
        SettingsProvider(),
        DatabaseAppProvider(),
        DatabaseRequestProvider(),
        SecurityProvider(),
        IdempotencyAppProvider(),
        IdempotencyRequestProvider(),
        RepositoriesProvider(),
        ServicesProvider(),
        MessagingProvider(),
    )


__all__ = [
    "DatabaseAppProvider",
    "DatabaseRequestProvider",
    "IdempotencyAppProvider",
    "IdempotencyRequestProvider",
    "MessagingProvider",
    "RepositoriesProvider",
    "SecurityProvider",
    "ServicesProvider",
    "SettingsProvider",
    "create_container",
]
