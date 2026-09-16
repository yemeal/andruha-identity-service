import logging

import pytest

from app.core.logging import _log_level, _service_context, setup_logging
from app.core.settings import Settings


def make_settings(*, dev_logs: bool, log_level: str = "INFO") -> Settings:
    return Settings(
        SERVICE_NAME="test-service",
        APP_VERSION="1.2.3",
        APP_ENVIRONMENT="test",
        HOST="127.0.0.1",
        PORT=9000,
        DEV_LOGS=dev_logs,
        LOG_LEVEL=log_level,
        MUTE_LOGGERS=("chatty",),
    )


def test_log_level_resolves_known_level() -> None:
    assert _log_level(make_settings(dev_logs=True, log_level="WARNING")) == 30


def test_log_level_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="Unsupported LOG_LEVEL"):
        _log_level(make_settings(dev_logs=True, log_level="LOUD"))


def test_service_context_adds_defaults_without_overwriting_values() -> None:
    processor = _service_context(make_settings(dev_logs=True))
    event = {"service": "already-bound"}

    result = processor(None, "info", event)

    assert result == {
        "service": "already-bound",
        "version": "1.2.3",
        "environment": "test",
    }


@pytest.mark.parametrize("dev_logs", [True, False])
def test_setup_logging_configures_root_and_muted_loggers(dev_logs: bool) -> None:
    logging.getLogger("chatty")
    logging.getLogger("chatty.child")
    logging.getLogger("sqlalchemy.engine.Engine")

    setup_logging(make_settings(dev_logs=dev_logs, log_level="DEBUG"))

    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("chatty").level == logging.WARNING
    assert logging.getLogger("chatty.child").level == logging.WARNING
    assert logging.getLogger("sqlalchemy.engine.Engine").level == logging.WARNING
    assert logging.getLogger("chatty").propagate is True


@pytest.mark.parametrize("dev_logs", [True, False])
@pytest.mark.parametrize("stdlib", [True, False])
def test_exception_chains_exclude_messages_parameters_and_validation_input(
    dev_logs, stdlib, capsys
) -> None:
    import structlog
    from pydantic import BaseModel, ValidationError
    from sqlalchemy.exc import IntegrityError

    class Input(BaseModel):
        count: int

    marker = "private-canary-31982"
    setup_logging(make_settings(dev_logs=dev_logs))
    try:
        try:
            Input(count=marker)
        except ValidationError as error:
            raise IntegrityError(
                "INSERT secret", {"password_hash": marker}, error
            ) from error
    except IntegrityError as error:
        try:
            raise RuntimeError(marker) from error
        except RuntimeError:
            if stdlib:
                logging.getLogger("safety-test").exception("operation failed")
            else:
                structlog.get_logger("safety-test").exception("operation failed")
    output = capsys.readouterr().err
    assert marker not in output
    assert "INSERT secret" not in output
    assert "IntegrityError" in output
    assert "ValidationError" in output
    assert "RuntimeError" in output
    assert "test_exception_chains" in output
