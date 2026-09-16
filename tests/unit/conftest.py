from __future__ import annotations

import structlog
from pytest import MonkeyPatch, fixture

from app.application.use_cases.login import handler


@fixture(autouse=True)
def fresh_login_logger(monkeypatch: MonkeyPatch) -> None:
    """Keep unit log capture isolated from integration logging bootstrap."""

    monkeypatch.setattr(handler, "logger", structlog.get_logger())
