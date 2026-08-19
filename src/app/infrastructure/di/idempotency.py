from collections.abc import AsyncIterator

import dishka
from dishka import Provider, Scope
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.idempotency import (
    DurableExecutionProtocol,
    HotIdempotencyStoreProtocol,
    IdempotencyCoordinatorProtocol,
    IdempotencyObserverProtocol,
    IdempotencyRecordRepositoryProtocol,
)
from app.application.ports.uow import AsyncUOWProtocol
from app.application.services.durable_idempotency import DurableExecutionService
from app.application.services.idempotency_coordinator import IdempotencyCoordinator
from app.core.settings import IdempotencySettings, ValkeySettings
from app.infrastructure.cache import ValkeyHotIdempotencyStore
from app.infrastructure.database.repositories.idempotency_record_repository import (
    IdempotencyRecordRepository,
)
from app.infrastructure.observability import PrometheusIdempotencyObserver
from app.infrastructure.resilience import IdempotencyCircuitBreaker
from app.infrastructure.resilience.circuit_breaking_hot_store import (
    CircuitBreakingHotStore,
)


class IdempotencyAppProvider(Provider):
    scope = Scope.APP

    @dishka.provide
    def idempotency_observer(self) -> IdempotencyObserverProtocol:
        return PrometheusIdempotencyObserver()

    @dishka.provide
    async def valkey(self, settings: ValkeySettings) -> AsyncIterator[Redis]:
        client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            settings.VALKEY_URL,
            decode_responses=False,
            max_connections=settings.VALKEY_MAX_CONNECTIONS,
            socket_timeout=settings.VALKEY_SOCKET_TIMEOUT,
            socket_connect_timeout=settings.VALKEY_SOCKET_CONNECT_TIMEOUT,
            health_check_interval=settings.VALKEY_HEALTH_CHECK_INTERVAL,
        )
        try:
            yield client
        finally:
            await client.aclose()

    @dishka.provide
    def circuit_breaker(
        self, settings: IdempotencySettings
    ) -> IdempotencyCircuitBreaker:
        return IdempotencyCircuitBreaker(
            settings.IDEMPOTENCY_CB_FAILURES,
            settings.IDEMPOTENCY_CB_RECOVERY_SECONDS,
            "identity-idempotency-valkey",
        )

    @dishka.provide
    def hot_store(
        self,
        client: Redis,
        circuit_breaker: IdempotencyCircuitBreaker,
        idempotency_settings: IdempotencySettings,
        valkey_settings: ValkeySettings,
    ) -> HotIdempotencyStoreProtocol:
        inner = ValkeyHotIdempotencyStore(
            client,
            result_ttl_seconds=idempotency_settings.IDEMPOTENCY_RESULT_TTL_SECONDS,
            key_namespace=valkey_settings.VALKEY_KEY_NAMESPACE,
        )
        return CircuitBreakingHotStore(inner, circuit_breaker)


class IdempotencyRequestProvider(Provider):
    scope = Scope.REQUEST

    @dishka.provide
    def records(
        self, session: AsyncSession, settings: IdempotencySettings
    ) -> IdempotencyRecordRepositoryProtocol:
        return IdempotencyRecordRepository(
            session, retention_seconds=settings.IDEMPOTENCY_RESULT_TTL_SECONDS
        )

    @dishka.provide
    def durable(
        self, records: IdempotencyRecordRepositoryProtocol, uow: AsyncUOWProtocol
    ) -> DurableExecutionProtocol:
        return DurableExecutionService(records, uow)

    @dishka.provide
    def coordinator(
        self,
        hot_store: HotIdempotencyStoreProtocol,
        durable: DurableExecutionProtocol,
        observer: IdempotencyObserverProtocol,
    ) -> IdempotencyCoordinatorProtocol:
        return IdempotencyCoordinator(hot_store, durable, observer=observer)
