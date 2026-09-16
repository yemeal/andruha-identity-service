import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from uuid import UUID

from httpx2 import AsyncClient, TransportError

from app.application.exceptions.profiles import (
    ProfileProvisioningRejectedError,
    ProfileProvisioningUnavailableError,
)
from app.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
)


class _ProfileCallError(Exception):
    def __init__(
        self, *, retryable: bool, retry_after_seconds: float | None = None
    ) -> None:
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


def _is_profile_dependency_failure(error: Exception) -> bool:
    return isinstance(error, _ProfileCallError) and error.retryable


class ProfileCircuitBreaker(CircuitBreaker):
    """Circuit identity dedicated to the synchronous User Profile dependency."""

    def __init__(self, fail_max: int, recovery_timeout: float) -> None:
        super().__init__(
            fail_max,
            recovery_timeout,
            "identity-user-profile",
            is_failure=_is_profile_dependency_failure,
        )


class HTTPProfileProvisioner:
    """Provision through a bounded retry protected by a dependency circuit."""

    def __init__(
        self,
        client: AsyncClient,
        *,
        configured: bool,
        circuit_breaker: ProfileCircuitBreaker,
        retry_delay_seconds: float,
        retry_max_delay_seconds: float = 1.0,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        if retry_max_delay_seconds <= 0:
            raise ValueError("retry_max_delay_seconds must be positive")
        if retry_delay_seconds > retry_max_delay_seconds:
            raise ValueError(
                "retry_delay_seconds must not exceed retry_max_delay_seconds"
            )
        self._client = client
        self._configured = configured
        self._circuit_breaker = circuit_breaker
        self._retry_delay_seconds = retry_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        self._sleeper = sleeper

    async def create_profile(self, user_id: UUID, registered_at: datetime) -> None:
        if not self._configured:
            raise ProfileProvisioningUnavailableError()

        payload = {"registered_at": registered_at.isoformat()}
        for attempt in range(2):
            try:
                await self._circuit_breaker.call(
                    self._put_profile,
                    user_id,
                    payload,
                )
                return
            except CircuitBreakerError:
                raise ProfileProvisioningUnavailableError() from None
            except _ProfileCallError as error:
                if not error.retryable:
                    raise ProfileProvisioningRejectedError() from None
                if attempt == 1:
                    break
                retry_after = error.retry_after_seconds or 0.0
                await self._sleeper(
                    min(
                        max(self._retry_delay_seconds, retry_after),
                        self._retry_max_delay_seconds,
                    )
                )

        raise ProfileProvisioningUnavailableError() from None

    async def _put_profile(self, user_id: UUID, payload: dict[str, str]) -> None:
        try:
            response = await self._client.put(
                f"/internal/v1/profiles/{user_id}", json=payload
            )
        except TransportError as error:
            raise _ProfileCallError(retryable=True) from error

        if response.status_code == 204:
            return
        retry_after_seconds: float | None = None
        if response.status_code in (423, 429):
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                with suppress(ValueError):
                    retry_after_seconds = max(float(retry_after), 0.0)
        raise _ProfileCallError(
            retryable=response.status_code in (423, 429, 500, 502, 503, 504),
            retry_after_seconds=retry_after_seconds,
        )
