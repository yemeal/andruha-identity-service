from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from uuid import uuid4

from fastapi.testclient import TestClient
from httpx import Response

DEFAULT_PASSWORD = "Integration-password-123"


@dataclass(frozen=True, slots=True)
class BrowserTokens:
    access_token: str
    refresh_token: str


def unique_email(prefix: str = "identity") -> str:
    return f"{prefix}-{uuid4()}@example.com"


def register(
    client: TestClient,
    *,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
) -> tuple[str, Response]:
    selected_email = email or unique_email()
    response = client.post(
        "/api/v1/auth/register",
        json={"email": selected_email, "password": password},
    )
    return selected_email, response


def login(
    client: TestClient,
    *,
    email: str,
    password: str = DEFAULT_PASSWORD,
) -> Response:
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )


def register_and_login(
    client: TestClient,
    *,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
) -> tuple[str, BrowserTokens]:
    selected_email, registration = register(
        client,
        email=email,
        password=password,
    )
    assert registration.status_code == 201, registration.text
    response = login(client, email=selected_email, password=password)
    assert response.status_code == 204, response.text
    return selected_email, tokens_from_response(response)


def tokens_from_response(response: Response) -> BrowserTokens:
    cookies = response.headers.get_list("set-cookie")
    parsed: dict[str, str] = {}
    for header in cookies:
        cookie = SimpleCookie()
        cookie.load(header)
        parsed.update({name: morsel.value for name, morsel in cookie.items()})
    return BrowserTokens(
        access_token=parsed["access_token"],
        refresh_token=parsed["refresh_token"],
    )


def refresh(
    client: TestClient,
    *,
    refresh_token: str,
    idempotency_key: str,
) -> Response:
    client.cookies.clear()
    return client.post(
        "/api/v1/auth/refresh",
        headers={
            "Cookie": f"refresh_token={refresh_token}",
            "Idempotency-Key": idempotency_key,
        },
    )


def me(client: TestClient, access_token: str) -> Response:
    return client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )


def safe_error(response: Response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    body: Mapping[str, object] = response.json()
    assert body["code"] == code
    serialized = response.text.lower()
    assert "password" not in serialized
    assert "traceback" not in serialized
