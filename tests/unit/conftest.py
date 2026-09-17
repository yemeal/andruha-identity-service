from __future__ import annotations

import structlog
from pytest import MonkeyPatch, fixture

from app.application.use_cases.login import handler as login_handler
from app.application.use_cases.logout import handler as logout_handler


@fixture(autouse=True)
def fresh_use_case_loggers(monkeypatch: MonkeyPatch) -> None:
    """Keep unit log capture isolated from integration logging bootstrap."""

    for handler in (login_handler, logout_handler):
        monkeypatch.setattr(handler, "logger", structlog.get_logger())
