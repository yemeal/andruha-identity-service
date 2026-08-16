"""Structured logging bootstrap for the service process."""

from collections.abc import MutableMapping
import logging
import logging.config
from typing import Any

import structlog

from app.core.settings import Settings, get_settings


EventDict = MutableMapping[str, Any]


def _log_level(settings: Settings) -> int:
    level = logging.getLevelName(settings.LOG_LEVEL)
    if not isinstance(level, int):
        raise ValueError(f"Unsupported LOG_LEVEL: {settings.LOG_LEVEL}")
    return level


def _service_context(settings: Settings):
    def add_service_context(
        _logger: object,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        event_dict.setdefault("service", settings.SERVICE_NAME)
        event_dict.setdefault("version", settings.APP_VERSION)
        event_dict.setdefault("environment", settings.APP_ENVIRONMENT)
        return event_dict

    return add_service_context


def setup_logging(settings: Settings | None = None) -> None:
    current_settings = settings or get_settings()
    level = _log_level(current_settings)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _service_context(current_settings),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
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

    for logger_name in logging.root.manager.loggerDict:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(
            max(level, logging.WARNING)
            if logger_name in current_settings.MUTE_LOGGERS
            else level
        )
