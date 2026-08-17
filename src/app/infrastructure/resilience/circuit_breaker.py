import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger()


def _always_failure(_error: Exception) -> bool:
    return True


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF-OPEN"


class CircuitBreakerError(Exception):
    def __init__(self, message: str = "Circuit Breaker открыт") -> None:
        super().__init__(message)


class CircuitBreaker:
    """
    Generic Circuit Breaker из Payment Service.

    Единственное усиление исходной реализации: в HALF_OPEN пропускается ровно
    один probe, чтобы восстановление Valkey не создавало новый stampede.
    """

    def __init__(
        self,
        fail_max: int,
        recovery_timeout: float,
        name: str,
        is_failure: Callable[[Exception], bool] | None = None,
    ) -> None:
        if fail_max <= 0:
            raise ValueError("fail_max must be positive")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be positive")
        self._fail_max = fail_max
        self._recovery_timeout = recovery_timeout
        self.name = name
        self._is_failure: Callable[[Exception], bool] = is_failure or _always_failure
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_progress = False
        self._probe_token: object | None = None
        self._generation = 0
        self._lock = asyncio.Lock()

    async def _check_state(self) -> None:
        if self._state is CircuitState.OPEN:
            now = time.monotonic()
            opened_at = self._opened_at
            if opened_at is not None and now - opened_at >= self._recovery_timeout:
                logger.info(
                    "circuit_breaker_cooldown_elapsed",
                    name=self.name,
                    opened_at=opened_at,
                    state=self._state,
                    recovery_timeout=self._recovery_timeout,
                )
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    "circuit_breaker_half_open",
                    name=self.name,
                    state=self._state,
                )

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._probe_in_progress = False
        self._probe_token = None
        self._generation += 1
        logger.error(
            "circuit_breaker_trip",
            name=self.name,
            recovery_timeout=self._recovery_timeout,
            state=self._state,
            opened_at=self._opened_at,
        )

    async def _handle_success(
        self,
        *,
        generation: int,
        probe_token: object | None,
    ) -> None:
        if (
            probe_token is not None
            and self._state is CircuitState.HALF_OPEN
            and self._probe_token is probe_token
        ):
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_progress = False
            self._probe_token = None
            self._generation += 1
            logger.info(
                "circuit_breaker_probe_request_succeeded",
                name=self.name,
                state=self._state,
            )
        elif (
            probe_token is None
            and self._state is CircuitState.CLOSED
            and self._generation == generation
        ):
            self._failure_count = 0

    async def _handle_failure(
        self,
        exc: Exception,
        *,
        generation: int,
        probe_token: object | None,
    ) -> None:
        is_current_probe = (
            probe_token is not None
            and self._state is CircuitState.HALF_OPEN
            and self._probe_token is probe_token
        )
        is_current_closed_call = (
            probe_token is None
            and self._state is CircuitState.CLOSED
            and self._generation == generation
        )
        if not is_current_probe and not is_current_closed_call:
            return

        self._failure_count += 1
        logger.warning(
            "circuit_breaker_recorded_failure",
            name=self.name,
            failure_count=self._failure_count,
            state=self._state,
            error_type=type(exc).__name__,
        )
        if is_current_closed_call:
            if self._failure_count >= self._fail_max:
                self._trip()
        elif is_current_probe:
            logger.warning(
                "circuit_breaker_probe_request_failed_in_half_open",
                name=self.name,
                failure_count=self._failure_count,
                state=self._state,
                error_type=type(exc).__name__,
            )
            self._trip()

    async def call[ResultT](
        self,
        func: Callable[..., Awaitable[ResultT]],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        probe_token: object | None = None
        async with self._lock:
            await self._check_state()
            if self._state is CircuitState.OPEN:
                opened_at = self._opened_at or time.monotonic()
                time_left = self._recovery_timeout - (time.monotonic() - opened_at)
                raise CircuitBreakerError(
                    f'Circuit Breaker "{self.name}" открыт. '
                    f"Осталось {max(time_left, 0):.1f} секунд"
                )
            if self._state is CircuitState.HALF_OPEN:
                if self._probe_in_progress:
                    raise CircuitBreakerError(
                        f'Circuit Breaker "{self.name}" выполняет пробный запрос'
                    )
                probe_token = object()
                self._probe_in_progress = True
                self._probe_token = probe_token
            generation = self._generation

        try:
            result = await func(*args, **kwargs)
        except asyncio.CancelledError:
            await self._release_probe(probe_token)
            raise
        except Exception as error:
            try:
                is_failure = self._is_failure(error)
            except BaseException:
                await self._release_probe(probe_token)
                raise
            if is_failure:
                async with self._lock:
                    await self._handle_failure(
                        error,
                        generation=generation,
                        probe_token=probe_token,
                    )
            else:
                # Классифицированный отказ провайдера подтверждает доступность
                # зависимости, поэтому закрывает HALF_OPEN и сбрасывает failures.
                async with self._lock:
                    await self._handle_success(
                        generation=generation,
                        probe_token=probe_token,
                    )
            raise
        except BaseException:
            await self._release_probe(probe_token)
            raise

        async with self._lock:
            await self._handle_success(
                generation=generation,
                probe_token=probe_token,
            )
        return result

    async def _release_probe(self, probe_token: object | None) -> None:
        if probe_token is None:
            return
        async with self._lock:
            if (
                self._state is CircuitState.HALF_OPEN
                and self._probe_token is probe_token
            ):
                self._probe_in_progress = False
                self._probe_token = None


class IdempotencyCircuitBreaker(CircuitBreaker):
    """DI identity for the optional Valkey idempotency acceleration path."""
