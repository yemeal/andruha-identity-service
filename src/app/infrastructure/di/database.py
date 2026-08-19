from collections.abc import AsyncIterator

import dishka
from dishka import Provider, Scope
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.ports.uow import AsyncUOWProtocol
from app.core.settings import PostgresSettings
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW


class DatabaseAppProvider(Provider):
    scope = Scope.APP

    @dishka.provide
    async def engine(self, settings: PostgresSettings) -> AsyncIterator[AsyncEngine]:
        """Обязательно закрываем engine при закрытии контейнера"""
        engine = create_async_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_MAX_OVERFLOW,
            pool_timeout=settings.DATABASE_POOL_TIMEOUT,
            pool_recycle=settings.DATABASE_POOL_RECYCLE,
        )
        try:
            yield engine
        finally:
            await engine.dispose()

    @dishka.provide
    def sessionmaker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(engine, expire_on_commit=False)


class DatabaseRequestProvider(Provider):
    scope = Scope.REQUEST

    @dishka.provide
    async def session(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    @dishka.provide
    def uow(self, session: AsyncSession) -> AsyncUOWProtocol:
        return SQLAlchemyAsyncUOW(session)
