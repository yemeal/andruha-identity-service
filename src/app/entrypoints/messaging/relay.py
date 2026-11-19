import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import signal
from types import FrameType

from aiokafka.errors import KafkaError
from faststream.kafka import KafkaBroker
import structlog

from app.application.outbox.relay import OutboxRelayService
from app.core.logging import setup_logging
from app.core.settings import OutboxSettings, Settings
from app.entrypoints.messaging.health import record_state
from app.infrastructure.di import create_container

logger = structlog.get_logger()


@asynccontextmanager
async def _broker_connection(
    broker: KafkaBroker,
    settings: OutboxSettings,
    shutdown_event: asyncio.Event,
) -> AsyncIterator[bool]:
    for attempt in range(1, settings.OUTBOX_BOOTSTRAP_MAX_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(
                broker.__aenter__(), timeout=settings.OUTBOX_BOOTSTRAP_TIMEOUT_SECONDS
            )
        except (KafkaError, OSError, TimeoutError) as error:
            await broker.__aexit__(type(error), error, error.__traceback__)
            if attempt == settings.OUTBOX_BOOTSTRAP_MAX_ATTEMPTS:
                raise
            delay = min(
                settings.OUTBOX_BOOTSTRAP_RETRY_INITIAL_SECONDS * 2 ** (attempt - 1),
                settings.OUTBOX_BOOTSTRAP_RETRY_MAX_SECONDS,
            )
            logger.warning(
                "kafka_bootstrap_retry",
                attempt=attempt,
                delay_seconds=delay,
                error_type=type(error).__name__,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            except TimeoutError:
                continue
            yield False
            return
        else:
            try:
                yield True
            finally:
                await broker.__aexit__(None, None, None)
            return


def _setup_signal_handlers(
    shutdown_event: asyncio.Event,
    relay: OutboxRelayService,
) -> None:
    """Устанавливает обработчики сигналов SIGINT и SIGTERM."""

    def _handle_signal(sig: int, _frame: FrameType | None) -> None:
        logger.info("outbox_relay_signal_received", signal=signal.Signals(sig).name)
        # 1. Говорим релею остановить опрос базы:
        relay.stop()
        # 2. Будим корутину ожидания:
        shutdown_event.set()

    # Регистрируем для Ctrl+C (локально)
    signal.signal(signal.SIGINT, _handle_signal)
    # Регистрируем для docker stop (в Linux/K8s)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)


async def main() -> None:
    # 1. Собираем DI-контейнер
    container = create_container()
    health_path = None

    try:
        # 2. Получаем настройки и настраиваем структурированный логгер
        settings = await container.get(Settings)
        health_path = settings.outbox.OUTBOX_HEALTH_STATE_PATH
        record_state(health_path, ready=False)
        setup_logging(settings.app)
        logger.info("outbox_relay_starting", environment=settings.app.APP_ENVIRONMENT)

        # 3. Разрешаем брокер и сервис релея из контейнера
        broker = await container.get(KafkaBroker)
        relay = await container.get(OutboxRelayService)

        # 4. Создаем Event остановки и настраиваем сигналы
        shutdown_event = asyncio.Event()
        _setup_signal_handlers(shutdown_event, relay)

        # 5. Открываем сетевые соединения FastStream Kafka
        async with _broker_connection(
            broker, settings.outbox, shutdown_event
        ) as connected:
            if not connected:
                return
            record_state(health_path, ready=True)
            logger.info(
                "kafka_broker_connected", servers=settings.kafka.KAFKA_BOOTSTRAP_SERVERS
            )

            # 6. Запускаем релей в фоновой таске
            relay_task = asyncio.create_task(
                relay.run(
                    poll_interval=settings.outbox.OUTBOX_POLL_INTERVAL_SECONDS,
                    batch_size=settings.outbox.OUTBOX_BATCH_SIZE,
                )
            )

            # 7. Засыпаем и ждем сигнала остановки
            await shutdown_event.wait()
            record_state(health_path, ready=False)
            logger.info("outbox_relay_initiating_graceful_shutdown")

            # 8. Даем воркеру время доотправить начатые сообщения
            try:
                await asyncio.wait_for(
                    relay_task,
                    timeout=settings.outbox.OUTBOX_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "outbox_relay_shutdown_timeout_exceeded",
                    timeout_seconds=settings.outbox.OUTBOX_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                pass

        logger.info("outbox_relay_stopped_cleanly")

    finally:
        if health_path is not None:
            health_path.unlink(missing_ok=True)
        # 9. Обязательно освобождаем все ресурсы контейнера при выходе
        await container.close()
        logger.info("outbox_relay_resources_released")


if __name__ == "__main__":
    asyncio.run(main())
