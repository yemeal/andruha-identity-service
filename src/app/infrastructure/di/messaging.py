from contextlib import asynccontextmanager

import dishka
from dishka import AsyncContainer, Provider, Scope
from faststream.kafka import KafkaBroker

from app.application.policies.outbox_retry import OutboxRetryPolicy
from app.application.ports.events.publisher import BrokerPublisherProtocol
from app.application.ports.outbox.scope_factory import (
    OutboxScope,
    OutboxScopeFactory,
)
from app.application.ports.repositories.outbox import OutboxRepositoryProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.services.outbox_relay import OutboxRelayService
from app.core.settings import KafkaSettings, OutboxSettings
from app.infrastructure.messaging.kafka_publisher import FastStreamKafkaPublisher


class MessagingProvider(Provider):
    scope = Scope.APP

    @dishka.provide
    def kafka_broker(self, settings: KafkaSettings) -> KafkaBroker:
        return KafkaBroker(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)

    @dishka.provide
    def broker_publisher(self, broker: KafkaBroker) -> BrokerPublisherProtocol:
        return FastStreamKafkaPublisher(broker=broker)

    @dishka.provide
    def outbox_scope_factory(self, container: AsyncContainer) -> OutboxScopeFactory:
        @asynccontextmanager
        async def factory():
            async with container() as request_container:
                uow = await request_container.get(AsyncUOWProtocol)
                repo = await request_container.get(OutboxRepositoryProtocol)
                yield OutboxScope(uow=uow, outbox_repo=repo)

        return factory

    @dishka.provide
    def outbox_relay_service(
        self,
        publisher: BrokerPublisherProtocol,
        scope_factory: OutboxScopeFactory,
        settings: OutboxSettings,
    ) -> OutboxRelayService:
        return OutboxRelayService(
            publisher=publisher,
            scope_factory=scope_factory,
            claim_lease_seconds=settings.OUTBOX_CLAIM_LEASE_SECONDS,
            retry_policy=OutboxRetryPolicy(
                initial_seconds=settings.OUTBOX_RETRY_INITIAL_SECONDS,
                max_seconds=settings.OUTBOX_RETRY_MAX_SECONDS,
                exponent=settings.OUTBOX_RETRY_EXPONENT,
                jitter_ratio=settings.OUTBOX_RETRY_JITTER_RATIO,
                max_attempts=settings.OUTBOX_RETRY_MAX_ATTEMPTS,
            ),
        )
