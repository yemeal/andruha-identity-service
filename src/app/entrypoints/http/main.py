from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from prometheus_client import (
    make_asgi_app,  # pyright: ignore[reportUnknownVariableType]
)
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp
import structlog

from app.application.ports.idempotency import (
    HotIdempotencyStoreProtocol,
    ReplayResultProtectorProtocol,
)
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    AccessTokenVerifierProtocol,
)
from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.entrypoints.http.middlewares import RequestIdMiddleware
from app.entrypoints.http.routers import create_api_router
from app.entrypoints.http.routers.exception_handlers import (
    create_internal_error_response,
    register_exception_handlers,
)
from app.entrypoints.http.routers.health import router as health_router
from app.infrastructure.di import create_container

logger = structlog.get_logger()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)
    container = create_container()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        try:
            await container.get(AccessTokenIssuerProtocol)
            await container.get(AccessTokenVerifierProtocol)
            await container.get(ReplayResultProtectorProtocol)
            await container.get(AsyncEngine)
            await container.get(Redis)
            await container.get(HotIdempotencyStoreProtocol)
            logger.info("application_started", version=app.version)
            yield
        finally:
            logger.info("application_shutting_down")

    app = FastAPI(
        title="Andruha Messenger / Identity Service",
        version=settings.app.APP_VERSION,
        lifespan=lifespan,
    )
    app.add_middleware(
        RequestIdMiddleware,
        internal_error_response_factory=create_internal_error_response,
    )
    app.include_router(health_router)
    app.include_router(
        create_api_router(
            include_test_token_endpoint=settings.test_token_endpoint_enabled
        )
    )
    register_exception_handlers(
        app,
        idempotency_retry_after_seconds=settings.idempotency.IDEMPOTENCY_LEASE_SECONDS,
    )
    app.mount("/metrics", cast(ASGIApp, make_asgi_app()))
    setup_dishka(container, app)
    return app
