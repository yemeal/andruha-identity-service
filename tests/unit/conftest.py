from __future__ import annotations

import structlog
from pytest import MonkeyPatch, fixture

from app.application.services import auth_service


@fixture(autouse=True)
def fresh_auth_service_logger(monkeypatch: MonkeyPatch) -> None:
    """Keep unit log capture isolated from integration logging bootstrap."""

    monkeypatch.setattr(auth_service, "logger", structlog.get_logger())
