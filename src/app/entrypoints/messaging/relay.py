import asyncio
import signal
from types import FrameType

from faststream.kafka import KafkaBroker
import structlog

from app.application.services.outbox_relay import OutboxRelayService
from app.core.logging import setup_logging
from app.core.settings import Settings
from app.infrastructure.di import create_container

logger = structlog.get_logger()


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

    try:
        # 2. Получаем настройки и настраиваем структурированный логгер
        settings = await container.get(Settings)
        setup_logging(settings.app)
        logger.info("outbox_relay_starting", environment=settings.app.APP_ENVIRONMENT)

        # 3. Разрешаем брокер и сервис релея из контейнера
        broker = await container.get(KafkaBroker)
        relay = await container.get(OutboxRelayService)

        # 4. Создаем Event остановки и настраиваем сигналы
        shutdown_event = asyncio.Event()
        _setup_signal_handlers(shutdown_event, relay)

        # 5. Открываем сетевые соединения FastStream Kafka
        async with broker:
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
        # 9. Обязательно освобождаем все ресурсы контейнера при выходе
        await container.close()
        logger.info("outbox_relay_resources_released")


if __name__ == "__main__":
    asyncio.run(main())
