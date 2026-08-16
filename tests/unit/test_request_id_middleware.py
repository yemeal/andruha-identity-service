import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.entrypoints.http.middlewares.request_id import RequestIdMiddleware
from app.entrypoints.http.middlewares.request_id_policy import (
    ValidatedRequestIdResolver,
)
from app.entrypoints.http.middlewares.request_lifecycle import (
    HttpRequestMetadata,
    HttpResponseState,
)


@dataclass
class RecordingContext:
    active: list[str] = field(default_factory=list)

    @contextmanager
    def bind(self, request_id: str) -> Iterator[None]:
        self.active.append(request_id)
        try:
            yield
        finally:
            self.active.remove(request_id)


@dataclass
class RecordingObserver:
    events: list[str] = field(default_factory=list)

    def started(self, request: HttpRequestMetadata) -> None:
        self.events.append(f"started:{request.request_id}")

    def failed(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
        error: Exception,
    ) -> None:
        self.events.append(f"failed:{type(error).__name__}")

    def disconnected(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
    ) -> None:
        self.events.append("disconnected")

    def finished(
        self,
        request: HttpRequestMetadata,
        response: HttpResponseState,
        duration_ms: float,
    ) -> None:
        self.events.append(f"finished:{response.status_code}:{response.failed}")


def http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "state": {"preserved": True},
    }


async def receive_request() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def make_middleware(
    app: ASGIApp,
    *,
    observer: RecordingObserver | None = None,
    context: RecordingContext | None = None,
) -> RequestIdMiddleware:
    return RequestIdMiddleware(
        app,
        resolver=ValidatedRequestIdResolver(generator=lambda: "generated-id"),
        observer=observer,
        request_context=context,
        clock=iter([1.0, 1.01234]).__next__,
    )


@pytest.mark.asyncio
async def test_middleware_adds_state_and_response_header() -> None:
    messages: list[Message] = []
    observer = RecordingObserver()
    context = RecordingContext()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["state"] == {
            "preserved": True,
            "request_id": "edge-request",
        }
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: Message) -> None:
        messages.append(message)

    middleware = make_middleware(app, observer=observer, context=context)
    await middleware(
        http_scope([(b"x-request-id", b"edge-request")]),
        receive_request,
        send,
    )

    response_start = messages[0]
    assert response_start["type"] == "http.response.start"
    assert (b"x-request-id", b"edge-request") in response_start["headers"]
    assert observer.events == ["started:edge-request", "finished:204:False"]
    assert context.active == []


@pytest.mark.asyncio
async def test_middleware_ignores_duplicate_request_id_headers() -> None:
    resolved_state: dict[str, object] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        resolved_state.update(scope["state"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: Message) -> None:
        return None

    middleware = make_middleware(app)
    await middleware(
        http_scope(
            [
                (b"x-request-id", b"first"),
                (b"x-request-id", b"second"),
            ]
        ),
        receive_request,
        send,
    )

    assert resolved_state["request_id"] == "generated-id"


@pytest.mark.asyncio
async def test_middleware_turns_pre_start_exception_into_safe_500() -> None:
    messages: list[Message] = []
    observer = RecordingObserver()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("private failure details")

    async def send(message: Message) -> None:
        messages.append(message)

    await make_middleware(app, observer=observer)(
        http_scope(),
        receive_request,
        send,
    )

    assert messages[0]["status"] == 500
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert (b"x-request-id", b"generated-id") in messages[0]["headers"]
    assert b"private failure details" not in messages[1]["body"]
    assert observer.events == [
        "started:generated-id",
        "failed:RuntimeError",
        "finished:500:True",
    ]


@pytest.mark.asyncio
async def test_middleware_fails_when_app_returns_without_response() -> None:
    messages: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    async def send(message: Message) -> None:
        messages.append(message)

    await make_middleware(app)(http_scope(), receive_request, send)

    assert messages[0]["status"] == 500


@pytest.mark.asyncio
async def test_middleware_reraises_exception_after_response_started() -> None:
    messages: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late failure")

    async def send(message: Message) -> None:
        messages.append(message)

    with pytest.raises(RuntimeError, match="late failure"):
        await make_middleware(app)(http_scope(), receive_request, send)

    assert len(messages) == 1


@pytest.mark.asyncio
async def test_middleware_preserves_cancellation() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise asyncio.CancelledError

    async def send(message: Message) -> None:
        return None

    with pytest.raises(asyncio.CancelledError):
        await make_middleware(app)(http_scope(), receive_request, send)


@pytest.mark.asyncio
async def test_middleware_preserves_client_disconnect() -> None:
    observer = RecordingObserver()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message: Message) -> None:
        raise OSError("client disconnected")

    with pytest.raises(OSError, match="client disconnected"):
        await make_middleware(app, observer=observer)(
            http_scope(),
            receive_request,
            send,
        )

    assert "disconnected" in observer.events


@pytest.mark.asyncio
async def test_middleware_bypasses_non_http_scope() -> None:
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await make_middleware(app)(scope, receive, send)

    assert called is True


@pytest.mark.parametrize(
    ("header_name", "state_key", "message"),
    [
        ("bad header", "request_id", "valid HTTP field name"),
        ("X-Request-Id", "", "state_key must not be empty"),
    ],
)
def test_middleware_rejects_invalid_configuration(
    header_name: str,
    state_key: str,
    message: str,
) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    with pytest.raises(ValueError, match=message):
        RequestIdMiddleware(app, header_name=header_name, state_key=state_key)
