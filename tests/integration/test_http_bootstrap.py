import re

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.entrypoints.http.main import create_app

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


@pytest.mark.integration
def test_health_endpoints_are_ready_inside_lifespan() -> None:
    with TestClient(create_app()) as client:
        live_response = client.get("/health/live")
        ready_response = client.get("/health/ready")

    assert live_response.status_code == 200
    assert live_response.json() == {"status": "ok"}
    assert ready_response.status_code == 200
    assert ready_response.json() == {"status": "ready"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_is_unavailable_without_started_lifespan() -> None:
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.integration
def test_direct_request_gets_safe_generated_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/live")

    request_id = response.headers["X-Request-Id"]
    assert REQUEST_ID_PATTERN.fullmatch(request_id) is not None


@pytest.mark.integration
def test_safe_internal_request_id_is_preserved() -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-Id": "internal-request:123"},
        )

    assert response.headers["X-Request-Id"] == "internal-request:123"


@pytest.mark.integration
def test_unsafe_request_id_is_replaced() -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-Id": "unsafe request id"},
        )

    assert response.headers["X-Request-Id"] != "unsafe request id"
    assert REQUEST_ID_PATTERN.fullmatch(response.headers["X-Request-Id"]) is not None


@pytest.mark.integration
def test_not_found_response_contains_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/not-found")

    assert response.status_code == 404
    assert "X-Request-Id" in response.headers


@pytest.mark.integration
def test_unhandled_error_is_safe_and_contains_request_id() -> None:
    app = create_app()

    @app.get("/_test/failure")
    async def fail_before_response() -> None:
        raise RuntimeError("private failure details")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/failure")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "X-Request-Id" in response.headers
    assert "private failure details" not in response.text


@pytest.mark.integration
def test_application_factory_returns_independent_instances() -> None:
    first = create_app()
    second = create_app()

    assert isinstance(first, FastAPI)
    assert isinstance(second, FastAPI)
    assert first is not second
