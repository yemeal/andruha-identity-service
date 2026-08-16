from dataclasses import dataclass, field

import pytest
from structlog.contextvars import get_contextvars

from app.entrypoints.http.middlewares.request_lifecycle import (
    HttpRequestMetadata,
    HttpResponseState,
    StructlogRequestContext,
    StructlogRequestLifecycleObserver,
)


@dataclass
class RecordingLogger:
    calls: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)

    def info(self, event: str, **event_fields: object) -> object:
        self.calls.append(("info", event, event_fields))
        return None

    def exception(self, event: str, **event_fields: object) -> object:
        self.calls.append(("exception", event, event_fields))
        return None


def test_response_state_tracks_success_and_server_failure() -> None:
    success = HttpResponseState()
    success.start(204)
    failure = HttpResponseState()
    failure.start(503)

    assert success.status_code == 204
    assert success.response_started is True
    assert success.failed is False
    assert failure.status_code == 503
    assert failure.failed is True


def test_response_state_rejects_second_start() -> None:
    response = HttpResponseState()
    response.start(200)

    with pytest.raises(RuntimeError, match="multiple response starts"):
        response.start(201)

    assert response.failed is True


def test_response_state_marks_pre_start_failure_and_disconnect() -> None:
    response = HttpResponseState(status_code=200)

    response.mark_failure()
    response.mark_client_disconnected()

    assert response.status_code == 500
    assert response.failed is True
    assert response.client_disconnected is True


def test_request_context_binds_and_always_clears() -> None:
    context = StructlogRequestContext()

    with context.bind("request-123"):
        assert get_contextvars() == {"request_id": "request-123"}

    assert get_contextvars() == {}

    with pytest.raises(RuntimeError, match="boom"), context.bind("request-456"):
        raise RuntimeError("boom")

    assert get_contextvars() == {}


def test_lifecycle_observer_emits_safe_structured_events() -> None:
    logger = RecordingLogger()
    observer = StructlogRequestLifecycleObserver(logger=logger)
    request = HttpRequestMetadata(
        request_id="request-123",
        method="GET",
        path="/health/live",
    )
    response = HttpResponseState()
    response.start(500)
    response.mark_client_disconnected()

    observer.started(request)
    observer.failed(request, response, RuntimeError("secret details"))
    observer.disconnected(request, response)
    observer.finished(request, response, 12.34)

    assert [event for _, event, _ in logger.calls] == [
        "request started",
        "request failed",
        "request disconnected",
        "request finished",
    ]
    failed_fields = logger.calls[1][2]
    assert failed_fields["error_type"] == "RuntimeError"
    assert "secret details" not in repr(logger.calls)
