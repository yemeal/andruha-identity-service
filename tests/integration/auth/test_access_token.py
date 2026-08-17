from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from tests.integration.conftest import IdentityInfrastructure
from tests.integration.helpers import (
    me,
    refresh,
    register_and_login,
    safe_error,
)

from app.core.settings import get_settings
from app.entrypoints.http.main import create_app

pytestmark = pytest.mark.integration


def _load_private_key(infrastructure: IdentityInfrastructure):
    return serialization.load_pem_private_key(
        infrastructure.jwt_private_key_path.read_bytes(),
        password=None,
    )


def _sign(
    infrastructure: IdentityInfrastructure,
    claims: dict[str, object],
    *,
    token_type: str = "at+jwt",
) -> str:
    return jwt.encode(
        claims,
        _load_private_key(infrastructure),
        algorithm="RS256",
        headers={"kid": "identity-v1", "typ": token_type},
    )


def test_issued_access_token_round_trips_through_real_http_pipeline(
    identity_client: TestClient,
) -> None:
    email, tokens = register_and_login(identity_client)

    response = me(identity_client, tokens.access_token)

    assert response.status_code == 200
    assert response.json()["email"] == email
    assert response.headers["Cache-Control"] == "no-store"
    assert "password" not in response.text.lower()


@pytest.mark.parametrize(
    ("authorization", "expected_code"),
    [
        (None, "auth.invalid_token"),
        ("Basic abc", "auth.invalid_token"),
        ("Bearer malformed.jwt", "auth.invalid_token"),
    ],
)
def test_missing_wrong_scheme_and_malformed_tokens_are_rejected(
    identity_client: TestClient,
    authorization: str | None,
    expected_code: str,
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}

    response = identity_client.get("/api/v1/auth/me", headers=headers)

    safe_error(response, 401, expected_code)


def test_token_signed_by_another_key_is_rejected(identity_client: TestClient) -> None:
    _email, issued = register_and_login(identity_client)
    claims = jwt.decode(issued.access_token, options={"verify_signature": False})
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        claims,
        attacker_key,
        algorithm="RS256",
        headers={"kid": "identity-v1", "typ": "at+jwt"},
    )

    safe_error(me(identity_client, forged), 401, "auth.invalid_token")


@pytest.mark.parametrize(
    ("claim_change", "token_type"),
    [
        ({"exp": datetime.now(UTC) - timedelta(seconds=1)}, "at+jwt"),
        ({"iat": datetime.now(UTC) + timedelta(minutes=5)}, "at+jwt"),
        ({"nbf": datetime.now(UTC) + timedelta(minutes=5)}, "at+jwt"),
        ({}, "refresh+jwt"),
    ],
)
def test_expired_future_issued_and_wrong_type_tokens_are_rejected(
    identity_client: TestClient,
    identity_infrastructure: IdentityInfrastructure,
    claim_change: dict[str, object],
    token_type: str,
) -> None:
    _email, issued = register_and_login(identity_client)
    claims = jwt.decode(issued.access_token, options={"verify_signature": False})
    claims.update(claim_change)
    token = _sign(identity_infrastructure, claims, token_type=token_type)

    safe_error(me(identity_client, token), 401, "auth.invalid_token")


def test_access_and_refresh_tokens_cannot_be_swapped(
    identity_client: TestClient,
) -> None:
    _email, tokens = register_and_login(identity_client)

    as_access = me(identity_client, tokens.refresh_token)
    as_refresh = refresh(
        identity_client,
        refresh_token=tokens.access_token,
        idempotency_key="access-as-refresh",
    )

    safe_error(as_access, 401, "auth.invalid_token")
    safe_error(as_refresh, 401, "auth.invalid_refresh_token")


def test_user_a_token_never_resolves_user_b(identity_client: TestClient) -> None:
    email_a, tokens_a = register_and_login(identity_client)
    email_b, _tokens_b = register_and_login(identity_client)

    response = me(identity_client, tokens_a.access_token)

    assert response.status_code == 200
    assert response.json()["email"] == email_a
    assert response.json()["email"] != email_b


def test_logout_does_not_revoke_stateless_access_token_before_expiry(
    identity_client: TestClient,
) -> None:
    email, tokens = register_and_login(identity_client)
    identity_client.cookies.clear()
    logout = identity_client.post(
        "/api/v1/auth/logout",
        headers={"Cookie": f"refresh_token={tokens.refresh_token}"},
    )

    response = me(identity_client, tokens.access_token)

    assert logout.status_code == 204
    assert response.status_code == 200
    assert response.json()["email"] == email


def test_access_token_is_valid_before_expiry_and_rejected_after_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "1")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), base_url="https://testserver") as client:
            _email, tokens = register_and_login(client)
            assert me(client, tokens.access_token).status_code == 200
            time.sleep(1.1)
            expired = me(client, tokens.access_token)
    finally:
        get_settings.cache_clear()

    safe_error(expired, 401, "auth.invalid_token")
