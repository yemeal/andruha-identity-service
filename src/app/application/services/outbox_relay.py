"""At-least-once transactional outbox relay worker.

Claims batches of messages using PostgreSQL FOR UPDATE SKIP LOCKED leases,
publishes each claimed message to Kafka (outside of database transactions),
and finalizes, retries, or quarantines messages in short individual transactions.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
import random
import uuid

import structlog

from app.application.exceptions import (
    PermanentPublishError,
    RetryExhaustedError,
)
from app.application.policies.outbox_retry import OutboxRetryPolicy
from app.application.ports.dto.outbox import OutboxMessage
from app.application.ports.events.publisher import BrokerPublisherProtocol
from app.application.ports.outbox.observer import OutboxRelayObserverProtocol
from app.application.ports.outbox.scope_factory import OutboxScopeFactory
from app.domain.base import utc_now

logger = structlog.get_logger(__name__)


class _NullOutboxRelayObserver(OutboxRelayObserverProtocol):
    """Приватная заглушка наблюдателя по умолчанию."""

    def batch_claimed(
        self,
        *,
        count: int,
        oldest_created_at: datetime,
        observed_at: datetime,
    ) -> None:
        pass

    def message_finalized(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
    ) -> None:
        pass

    def message_retry_scheduled(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
        attempt: int,
        delay_seconds: float,
    ) -> None:
        pass

    def message_quarantined(
        self,
        *,
        message_id: uuid.UUID,
        message_type: str,
        error_class: str,
    ) -> None:
        pass


class OutboxRelayService:
    """
    Высокопроизводительный конкурентный сервис Outbox Relay.

    - Захватывает пачку сообщений через FOR UPDATE SKIP LOCKED.
    - Параллельно отправляет сообщения с разными ключами в брокер.
    - Сохраняет строгий порядок FIFO для сообщений с одинаковым key.
    - Переводит успешные сообщения в SUCCESS, сбои — в RETRY или QUARANTINED.
    """

    def __init__(
        self,
        publisher: BrokerPublisherProtocol,
        scope_factory: OutboxScopeFactory,
        *,
        observer: OutboxRelayObserverProtocol | None = None,
        claim_lease_seconds: float = 30.0,
        retry_policy: OutboxRetryPolicy | None = None,
        now_factory: Callable[[], datetime] = utc_now,
        jitter_source: Callable[[], float] | None = None,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")

        self._publisher = publisher
        self._scope_factory = scope_factory
        self._observer = observer or _NullOutboxRelayObserver()
        self._claim_lease_seconds = claim_lease_seconds
        self._retry_policy = retry_policy or OutboxRetryPolicy()
        self._now = now_factory
        self._jitter_source = jitter_source or (lambda: random.uniform(-1.0, 1.0))
        self._stop_event = asyncio.Event()

    async def run_once(self, batch_size: int = 50) -> int:
        """
        Выполнить одну итерацию захвата и публикации пачки сообщений.

        :param batch_size: Максимальный размер пачки.
        :return: Количество обработанных сообщений.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        owner_token = uuid.uuid4()
        claimed_at = self._now()
        claim_expires_at = claimed_at + timedelta(seconds=self._claim_lease_seconds)

        messages = await self._claim_batch(
            owner_token=owner_token,
            claimed_at=claimed_at,
            claim_expires_at=claim_expires_at,
            batch_size=batch_size,
        )
        if not messages:
            return 0

        oldest_created = min(m.created_at for m in messages)
        self._observer.batch_claimed(
            count=len(messages),
            oldest_created_at=oldest_created,
            observed_at=claimed_at,
        )

        # Группируем по partition key для соблюдения FIFO по ключу
        by_key: dict[str, list[OutboxMessage]] = defaultdict(list)
        for msg in messages:
            by_key[msg.key].append(msg)

        async def _process_key_stream(
            key_messages: list[OutboxMessage],
        ) -> None:
            for msg in key_messages:
                if self._stop_event.is_set():
                    break
                await self._publish_claimed(msg, owner_token=owner_token)

        # Конкурентная обработка независимых ключей
        tasks = [_process_key_stream(key_msgs) for key_msgs in by_key.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

        return len(messages)

    async def run(
        self,
        poll_interval: float = 2.0,
        batch_size: int = 50,
    ) -> None:
        """
        Бесконечный цикл релея с graceful shutdown.

        :param poll_interval: Задержка между опросами, если очередь пуста.
        :param batch_size: Размер пачки.
        """
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")

        self._stop_event.clear()
        logger.info("outbox_relay_loop_started")

        while not self._stop_event.is_set():
            try:
                processed = await self.run_once(batch_size=batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "outbox_relay_iteration_failed",
                    error_type=type(error).__name__,
                )
                processed = 0

            if self._stop_event.is_set():
                break

            # Если пачка была полной, пробуем сразу следующую без задержки;
            # если сообщений не было — ждем poll_interval
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=poll_interval,
                    )
                except TimeoutError:
                    continue

        logger.info("outbox_relay_loop_stopped")

    def stop(self) -> None:
        """Установить сигнал плавной остановки воркера."""
        self._stop_event.set()

    async def _claim_batch(
        self,
        *,
        owner_token: uuid.UUID,
        claimed_at: datetime,
        claim_expires_at: datetime,
        batch_size: int,
    ) -> list[OutboxMessage]:
        async with self._scope_factory() as scope, scope.uow:
            return await scope.outbox_repo.claim_batch(
                owner_token=owner_token,
                claimed_at=claimed_at,
                claim_expires_at=claim_expires_at,
                limit=batch_size,
            )

    async def _publish_claimed(
        self,
        message: OutboxMessage,
        *,
        owner_token: uuid.UUID,
    ) -> None:
        try:
            await self._publisher.publish(message)
        except PermanentPublishError as error:
            await self._quarantine(
                message,
                owner_token=owner_token,
                error_class=type(error).__name__,
            )
            return
        except Exception as error:
            # Проверка на исчерпание лимита попыток ретрая
            attempt = message.attempts + 1
            if attempt >= self._retry_policy.max_attempts:
                await self._quarantine(
                    message,
                    owner_token=owner_token,
                    error_class=RetryExhaustedError.__name__,
                )
                return

            await self._schedule_retry(
                message,
                owner_token=owner_token,
                error=error,
            )
            return

        # Успешная отправка -> переводим в SUCCESS
        published_at = self._now()
        async with self._scope_factory() as scope, scope.uow:
            finalized = await scope.outbox_repo.finalize_published(
                message.id,
                owner_token=owner_token,
                published_at=published_at,
            )

        if finalized:
            self._observer.message_finalized(
                message_id=message.id,
                message_type=message.type,
            )

    async def _schedule_retry(
        self,
        message: OutboxMessage,
        *,
        owner_token: uuid.UUID,
        error: Exception,
    ) -> None:
        attempt = message.attempts + 1
        delay = self._retry_policy.delay_seconds(
            attempt=attempt,
            jitter_sample=self._jitter_source(),
        )
        available_at = self._now() + timedelta(seconds=delay)
        error_class = type(error).__name__

        async with self._scope_factory() as scope, scope.uow:
            scheduled = await scope.outbox_repo.schedule_retry(
                message.id,
                owner_token=owner_token,
                available_at=available_at,
                error_class=error_class,
            )

        if scheduled:
            self._observer.message_retry_scheduled(
                message_id=message.id,
                message_type=message.type,
                attempt=attempt,
                delay_seconds=delay,
            )

    async def _quarantine(
        self,
        message: OutboxMessage,
        *,
        owner_token: uuid.UUID,
        error_class: str,
    ) -> None:
        quarantined_at = self._now()

        async with self._scope_factory() as scope, scope.uow:
            quarantined = await scope.outbox_repo.quarantine(
                message.id,
                owner_token=owner_token,
                quarantined_at=quarantined_at,
                error_class=error_class,
            )

        if quarantined:
            self._observer.message_quarantined(
                message_id=message.id,
                message_type=message.type,
                error_class=error_class,
            )
