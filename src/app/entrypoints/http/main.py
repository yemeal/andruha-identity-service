"""FastAPI application factory for the operational HTTP skeleton."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.entrypoints.http.middlewares import RequestIdMiddleware
from app.entrypoints.http.routers.health import router as health_router


logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ready = True
    logger.info("application started", version=app.version)
    try:
        yield
    finally:
        app.state.ready = False
        logger.info("application shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)

    app = FastAPI(
        title="Andruha Messenger / Identity Service",
        version=settings.APP_VERSION,
        lifespan=lifespan,
    )
    app.state.ready = False
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health_router)
    return app
