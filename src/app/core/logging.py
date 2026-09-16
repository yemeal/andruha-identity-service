"""Structured logging bootstrap for the service process."""

from collections.abc import MutableMapping
import logging
import logging.config
import sys
import traceback
from typing import Any

import structlog

from app.core.settings import AppSettings, Settings, get_settings

EventDict = MutableMapping[str, Any]


def _log_level(settings: AppSettings | Settings) -> int:
    app_settings = settings.app if isinstance(settings, Settings) else settings
    level = logging.getLevelNamesMapping().get(app_settings.LOG_LEVEL)
    if level is None:
        raise ValueError(f"Unsupported LOG_LEVEL: {app_settings.LOG_LEVEL}")
    return level


def _service_context(settings: AppSettings | Settings):
    app_settings = settings.app if isinstance(settings, Settings) else settings

    def add_service_context(
        _logger: object,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict.setdefault("service", app_settings.SERVICE_NAME)
        event_dict.setdefault("version", app_settings.APP_VERSION)
        event_dict.setdefault("environment", app_settings.APP_ENVIRONMENT)
        return event_dict

    return add_service_context


def _safe_exception_info(_logger: object, _method: str, event: EventDict) -> EventDict:
    """Keep exception types and frames without messages, source code, or locals."""
    info = event.pop("exc_info", None)
    if not info:
        return event
    error = (
        info
        if isinstance(info, BaseException)
        else (info[1] if isinstance(info, tuple) else sys.exc_info()[1])
    )
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        seen.add(id(error))
        frames = [
            {
                "file": frame.f_code.co_filename,
                "line": line,
                "function": frame.f_code.co_name,
            }
            for frame, line in traceback.walk_tb(error.__traceback__)
        ]
        chain.append({"type": type(error).__name__, "frames": frames})
        error = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
    event["exception_chain"] = chain
    return event


def setup_logging(settings: AppSettings | Settings | None = None) -> None:
    current_settings = (
        settings.app
        if isinstance(settings, Settings)
        else (settings or get_settings().app)
    )
    level = _log_level(current_settings)
    # Stdlib formatter failures must not print the original exception to stderr.
    logging.raiseExceptions = False

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _service_context(current_settings),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _safe_exception_info,
    ]
    renderer = (
        structlog.dev.ConsoleRenderer()
        if current_settings.DEV_LOGS
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structured": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "foreign_pre_chain": shared_processors,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        renderer,
                    ],
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "structured",
                }
            },
            "root": {
                "handlers": ["default"],
                "level": level,
            },
        }
    )

    muted_loggers = (*current_settings.MUTE_LOGGERS, "sqlalchemy")
    logger_names = {*logging.root.manager.loggerDict, *muted_loggers}
    for logger_name in logger_names:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        is_muted = any(
            logger_name == muted or logger_name.startswith(f"{muted}.")
            for muted in muted_loggers
        )
        logger.setLevel(max(level, logging.WARNING) if is_muted else logging.NOTSET)
