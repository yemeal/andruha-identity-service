from fastapi import APIRouter

from app.entrypoints.http.routers.v1 import create_v1_router


def create_api_router(*, include_test_token_endpoint: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api")
    router.include_router(
        create_v1_router(include_test_token_endpoint=include_test_token_endpoint)
    )
    return router


__all__ = ["create_api_router"]
