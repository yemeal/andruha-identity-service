from dishka import AsyncContainer, make_async_container

from app.core.settings import Settings
from app.infrastructure.di.provider import (
    AppProvider,
    RequestProvider,
    SettingsProvider,
)


def create_container(settings: Settings) -> AsyncContainer:
    return make_async_container(
        SettingsProvider(settings), AppProvider(), RequestProvider()
    )


__all__ = ["create_container"]
