from fastapi import APIRouter

from app.entrypoints.http.routers.v1.auth import create_auth_router


def create_v1_router(
    *,
    include_test_token_endpoint: bool = False,
) -> APIRouter:
    v1_router = APIRouter(prefix="/v1")
    v1_router.include_router(
        create_auth_router(
            include_test_token_endpoint=include_test_token_endpoint,
        )
    )
    return v1_router
