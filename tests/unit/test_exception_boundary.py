import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from app.application.exceptions.commands import InvalidCommandError
from app.application.exceptions.persistence import (
    PersistenceIntegrityError,
    PersistenceUnavailableError,
)
from app.application.exceptions.security import (
    InvalidTokenConfigurationError,
    InvalidTokenSigningKeyError,
    TokenIssuanceError,
)
from app.domain.exceptions import (
    InvalidCredentialsError,
    InvalidEmailError,
    InvalidPasswordError,
)
from app.entrypoints.http.middlewares import RequestIdMiddleware
from app.core.logging import setup_logging
from app.core.settings import AppSettings
from app.entrypoints.http.routers.exception_handlers import (
    create_internal_error_response,
    register_exception_handlers,
)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidCredentialsError(), 401, "auth.invalid_credentials"),
        (InvalidCommandError(), 422, "request.validation_error"),
        (InvalidEmailError(), 422, "request.validation_error"),
        (InvalidPasswordError(), 422, "request.validation_error"),
        (InvalidTokenConfigurationError(), 500, "request.internal_error"),
        (InvalidTokenSigningKeyError(), 500, "request.internal_error"),
        (TokenIssuanceError(), 500, "request.internal_error"),
        (PersistenceIntegrityError(), 500, "request.internal_error"),
        (PersistenceUnavailableError(), 500, "request.internal_error"),
        (
            IntegrityError(
                "SQL secret", {"password": "canary-93"}, ValueError("canary-93")
            ),
            500,
            "request.internal_error",
        ),
    ],
)
async def test_http_boundary_uses_fixed_public_errors(
    error, status, code, capsys
) -> None:
    setup_logging(AppSettings(_env_file=None, DEV_LOGS=False))
    app = FastAPI()
    app.add_middleware(
        RequestIdMiddleware,
        internal_error_response_factory=create_internal_error_response,
    )
    register_exception_handlers(app)

    @app.get("/failure")
    async def failure():
        raise error

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/failure")
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "canary-93" not in response.text
    assert "SQL" not in response.text
    assert "traceback" not in response.text.lower()
    assert response.headers["cache-control"] == "no-store"
    assert "x-request-id" in response.headers
    output = capsys.readouterr()
    assert "canary-93" not in output.out + output.err
