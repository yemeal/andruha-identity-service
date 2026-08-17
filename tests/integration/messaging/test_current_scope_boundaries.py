from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.helpers import register

from app.infrastructure.database.models import Base, UserORM

pytestmark = pytest.mark.integration


async def test_registration_is_not_coupled_to_kafka_in_current_phase(
    identity_client: TestClient,
    database_session: AsyncSession,
) -> None:
    email, response = register(identity_client)

    assert response.status_code == 201
    assert await database_session.scalar(
        select(UserORM.id).where(UserORM.email == email)
    )
    assert "outbox" not in Base.metadata.tables


def test_kafka_outbox_and_session_management_remain_explicit_future_scope(
    identity_client: TestClient,
) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = " ".join(project["project"]["dependencies"]).lower()

    assert "kafka" not in dependencies
    assert "outbox" not in Base.metadata.tables
    assert identity_client.post("/api/v1/auth/logout-all").status_code == 404
