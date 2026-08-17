import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.entrypoints.http.main import create_app

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


@pytest.fixture(autouse=True)
def valid_local_key_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "jwt-private.pem"
    public_path = tmp_path / "jwt-public.pem"
    replay_path = tmp_path / "replay.key"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    replay_path.write_bytes(b"r" * 32)
    monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(private_path))
    monkeypatch.setenv("JWT_PUBLIC_KEY_PATH", str(public_path))
    monkeypatch.setenv("REPLAY_ENCRYPTION_KEY_PATHS", f"replay-v1={replay_path}")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.integration
def test_readiness_requires_postgres_but_liveness_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.entrypoints.http.routers.health.check_postgres",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.entrypoints.http.routers.health.check_valkey",
        AsyncMock(return_value=True),
    )
    with TestClient(create_app()) as client:
        live_response = client.get("/health/live")
        ready_response = client.get("/health/ready")

    assert live_response.status_code == 200
    assert live_response.json() == {"status": "ok"}
    assert ready_response.status_code == 503
    assert ready_response.json()["postgres"] == "unavailable"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_readiness_degrades_when_only_valkey_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.entrypoints.http.routers.health.check_postgres",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.entrypoints.http.routers.health.check_valkey",
        AsyncMock(return_value=False),
    )
    transport = httpx2.ASGITransport(app=create_app())
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "postgres": "ok",
        "valkey": "degraded",
    }


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
    assert response.json() == {
        "code": "request.internal_error",
        "detail": "internal server error",
    }
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
