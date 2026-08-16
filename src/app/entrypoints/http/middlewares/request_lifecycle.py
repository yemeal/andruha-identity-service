"""Observability adapters for a request-scoped ASGI lifecycle."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Protocol

import structlog
from structlog.contextvars import bound_contextvars, clear_contextvars


class RequestContext(Protocol):
    def bind(self, request_id: str) -> AbstractContextManager[None]:
        """Bind a request ID for one ASGI invocation."""
        ...


@dataclass(frozen=True, slots=True)
class StructlogRequestContext:
    field_name: str = "request_id"

    @contextmanager
    def bind(self, request_id: str) -> Iterator[None]:
        clear_contextvars()
        try:
            with bound_contextvars(**{self.field_name: request_id}):
                yield
        finally:
            clear_contextvars()


@dataclass(frozen=True, slots=True)
class HttpRequestMetadata:
    request_id: str
    method: str
    path: str


@dataclass(slots=True)
class HttpResponseState:
    status_code: int = 500
    response_started: bool = False
    failed: bool = False
    client_disconnected: bool = False

    def start(self, status_code: int) -> None:
        if self.response_started:
            self.mark_failure()
            raise RuntimeError("ASGI application sent multiple response starts")
        self.response_started = True
        self.status_code = status_code
        self.failed = self.failed or status_code >= 500

    def mark_failure(self) -> None:
        self.failed = True
        if not self.response_started:
            self.status_code = 500

    def mark_client_disconnected(self) -> None:
        self.failed = True
        self.client_disconnected = True


class RequestLifecycleObserver(Protocol):
    def started(self, request: HttpRequestMetadata) -> None: ...

    def failed(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
        error: Exception,
    ) -> None: ...

    def disconnected(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
    ) -> None: ...

    def finished(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
        duration_ms: float,
    ) -> None: ...


class EventLogger(Protocol):
    def info(self, event: str, **event_fields: object) -> object: ...

    def exception(self, event: str, **event_fields: object) -> object: ...


@dataclass(frozen=True, slots=True)
class StructlogRequestLifecycleObserver:
    logger: EventLogger = field(default_factory=structlog.get_logger)

    def started(self, request: HttpRequestMetadata) -> None:
        self.logger.info(
            "request started",
            method=request.method,
            path=request.path,
        )

    def failed(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
        error: Exception,
    ) -> None:
        self.logger.exception(
            "request failed",
            method=request.method,
            path=request.path,
            response_started=response.response_started,
            error_type=type(error).__name__,
        )

    def disconnected(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
    ) -> None:
        self.logger.info(
            "request disconnected",
            method=request.method,
            path=request.path,
            response_started=response.response_started,
        )

    def finished(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
        duration_ms: float,
    ) -> None:
        self.logger.info(
            "request finished",
            method=request.method,
            path=request.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            failed=response.failed,
            client_disconnected=response.client_disconnected,
        )
