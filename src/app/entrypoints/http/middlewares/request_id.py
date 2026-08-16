"""Pure ASGI middleware for the request correlation boundary."""

import asyncio
from collections.abc import Callable
import re
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.entrypoints.http.middlewares.request_id_policy import (
    RequestIdResolver,
    ValidatedRequestIdResolver,
)
from app.entrypoints.http.middlewares.request_lifecycle import (
    HttpRequestMetadata,
    HttpResponseState,
    RequestContext,
    RequestLifecycleObserver,
    StructlogRequestContext,
    StructlogRequestLifecycleObserver,
)


Clock = Callable[[], float]
InternalErrorResponseFactory = Callable[[], Response]

_HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


def _default_internal_error_response() -> Response:
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error"},
        headers={"Cache-Control": "no-store"},
    )


class RequestIdMiddleware:
    """Bind, return, log, and always clear one safe request ID."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        resolver: RequestIdResolver | None = None,
        request_context: RequestContext | None = None,
        observer: RequestLifecycleObserver | None = None,
        internal_error_response_factory: InternalErrorResponseFactory = (
            _default_internal_error_response
        ),
        header_name: str = "X-Request-Id",
        state_key: str = "request_id",
        clock: Clock = time.perf_counter,
    ) -> None:
        if _HTTP_HEADER_NAME.fullmatch(header_name) is None:
            raise ValueError("header_name must be a valid HTTP field name")
        if not state_key:
            raise ValueError("state_key must not be empty")

        self._app = app
        self._resolver = resolver or ValidatedRequestIdResolver()
        self._request_context = request_context or StructlogRequestContext()
        self._observer = (
            observer or StructlogRequestLifecycleObserver()
        )
        self._internal_error_response_factory = (
            internal_error_response_factory
        )
        self._header_name = header_name
        self._state_key = state_key
        self._clock = clock

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        request_scope = self._copy_scope(scope, request_id)
        request = HttpRequestMetadata(
            request_id=request_id,
            method=scope.get("method", ""),
            path=scope.get("path", ""),
        )
        response = HttpResponseState()
        started_at = self._clock()

        async def send_with_request_id(message: Message) -> None:
            outbound_message = message

            if message["type"] == "http.response.start":
                response.start(message["status"])
                outbound_message = dict(message)
                MutableHeaders(scope=outbound_message)[
                    self._header_name
                ] = request_id

            try:
                await send(outbound_message)
            except OSError:
                response.mark_client_disconnected()
                self._observer.disconnected(request, response)
                raise

        with self._request_context.bind(request_id):
            self._observer.started(request)
            try:
                await self._app(
                    request_scope,
                    receive,
                    send_with_request_id,
                )
                if not response.response_started:
                    raise RuntimeError(
                        "ASGI application returned without a response"
                    )
            except asyncio.CancelledError:
                response.mark_failure()
                raise
            except Exception as error:
                response.mark_failure()
                if response.client_disconnected:
                    raise

                self._observer.failed(request, response, error)
                if response.response_started:
                    raise

                error_response = self._internal_error_response_factory()
                await error_response(
                    request_scope,
                    receive,
                    send_with_request_id,
                )
            finally:
                if not response.response_started:
                    response.mark_failure()
                self._observer.finished(
                    request,
                    response,
                    self._duration_ms(started_at),
                )

    def _resolve_request_id(self, scope: Scope) -> str:
        candidates = Headers(scope=scope).getlist(self._header_name)
        candidate = candidates[0] if len(candidates) == 1 else None
        return self._resolver.resolve(candidate)

    def _copy_scope(self, scope: Scope, request_id: str) -> Scope:
        request_scope = dict(scope)
        request_state = dict(scope.get("state") or {})
        request_state[self._state_key] = request_id
        request_scope["state"] = request_state
        return request_scope

    def _duration_ms(self, started_at: float) -> float:
        return round((self._clock() - started_at) * 1000, 2)
