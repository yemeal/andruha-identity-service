from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import (
    DEFAULT_PASSWORD,
    login,
    register,
    register_and_login,
    tokens_from_response,
    unique_email,
)

from app.domain.users import UserStatus
from app.infrastructure.database.models import (
    AuthSessionORM,
    RefreshTokenORM,
    UserORM,
)

pytestmark = pytest.mark.integration


async def test_login_crosses_argon2_jwt_and_real_persistence(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email, registration = register(identity_client)
    assert registration.status_code == 201

    response = login(identity_client, email=email)

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["Cache-Control"] == "no-store"
    tokens = tokens_from_response(response)
    claims = jwt.decode(tokens.access_token, options={"verify_signature": False})
    user = await database_session.scalar(select(UserORM).where(UserORM.email == email))
    assert user is not None
    assert claims["sub"] == str(user.id)
    assert claims["iss"] == "andruha-identity-service"
    assert claims["role"] == "USER"
    assert set(claims) >= {"sub", "exp", "iat", "jti", "aud", "iss", "role"}
    assert claims["exp"] > claims["iat"]

    sessions = list(
        await database_session.scalars(
            select(AuthSessionORM).where(AuthSessionORM.user_id == user.id)
        )
    )
    assert len(sessions) == 1
    stored_tokens = list(
        await database_session.scalars(
            select(RefreshTokenORM).where(RefreshTokenORM.session_id == sessions[0].id)
        )
    )
    assert len(stored_tokens) == 1
    assert stored_tokens[0].token_hash != tokens.refresh_token.encode()
    assert len(stored_tokens[0].token_hash) == 32


def test_unknown_email_and_wrong_password_are_indistinguishable_over_http(
    identity_client: TestClient,
) -> None:
    email, registration = register(identity_client)
    assert registration.status_code == 201

    wrong_password = login(
        identity_client,
        email=email,
        password="Wrong-password-123",
    )
    unknown_email = login(
        identity_client,
        email=unique_email("unknown"),
        password="Wrong-password-123",
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.headers["Cache-Control"] == "no-store"
    assert unknown_email.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in wrong_password.headers
    assert "set-cookie" not in unknown_email.headers


async def test_disabled_user_cannot_login_and_creates_no_session(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email, registration = register(identity_client)
    assert registration.status_code == 201
    await database_session.execute(
        update(UserORM).where(UserORM.email == email).values(status=UserStatus.DISABLED)
    )
    await database_session.commit()

    response = login(identity_client, email=email)

    assert response.status_code == 401
    assert response.json()["code"] == "auth.invalid_credentials"
    assert (
        await database_session.scalar(select(func.count()).select_from(AuthSessionORM))
        == 0
    )


async def test_each_login_creates_an_independent_session_family(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email, first_tokens = register_and_login(identity_client)
    second = login(identity_client, email=email, password=DEFAULT_PASSWORD)
    third = login(identity_client, email=email, password=DEFAULT_PASSWORD)

    assert second.status_code == third.status_code == 204
    second_tokens = tokens_from_response(second)
    third_tokens = tokens_from_response(third)
    assert (
        len(
            {
                first_tokens.refresh_token,
                second_tokens.refresh_token,
                third_tokens.refresh_token,
            }
        )
        == 3
    )
    user_id = await database_session.scalar(
        select(UserORM.id).where(UserORM.email == email)
    )
    assert user_id is not None
    assert (
        await database_session.scalar(
            select(func.count())
            .select_from(AuthSessionORM)
            .where(AuthSessionORM.user_id == user_id)
        )
        == 3
    )
