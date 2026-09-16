import argparse
import asyncio
from contextlib import suppress
import signal
from types import FrameType
from uuid import UUID

import structlog

from app.application.services.registration import RegistrationAdministrationProtocol
from app.application.services.registration_reconciler import (
    RegistrationReconcilerProtocol,
)
from app.core.logging import setup_logging
from app.core.settings import Settings
from app.infrastructure.di import create_container

logger = structlog.get_logger(__name__)


def _setup_signal_handlers(
    shutdown_event: asyncio.Event,
    reconciler: RegistrationReconcilerProtocol,
) -> None:
    def _handle_signal(sig: int, _frame: FrameType | None) -> None:
        logger.info(
            "registration_reconciler_signal_received",
            signal=signal.Signals(sig).name,
        )
        reconciler.stop()
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)


async def main(*, redrive_operation_id: str | None = None) -> None:
    container = create_container()
    try:
        settings = await container.get(Settings)
        setup_logging(settings.app)
        if redrive_operation_id is not None:
            administration = await container.get(RegistrationAdministrationProtocol)
            redriven = await administration.redrive(UUID(redrive_operation_id))
            logger.info(
                "registration_redrive_finished",
                operation_id=redrive_operation_id,
                redriven=redriven,
            )
            return
        reconciler = await container.get(RegistrationReconcilerProtocol)
        shutdown_event = asyncio.Event()
        _setup_signal_handlers(shutdown_event, reconciler)

        task = asyncio.create_task(
            reconciler.run(
                poll_interval=(
                    settings.registration.REGISTRATION_POLL_INTERVAL_SECONDS
                ),
                batch_size=settings.registration.REGISTRATION_BATCH_SIZE,
            )
        )
        shutdown_waiter = asyncio.create_task(shutdown_event.wait())
        done, _pending = await asyncio.wait(
            {task, shutdown_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            shutdown_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await shutdown_waiter
            await task
            return
        try:
            await asyncio.wait_for(
                task,
                timeout=(settings.registration.REGISTRATION_SHUTDOWN_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            logger.warning("registration_reconciler_shutdown_timeout")
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            pass
    finally:
        await container.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--redrive", dest="redrive_operation_id")
    args = parser.parse_args()
    asyncio.run(main(redrive_operation_id=args.redrive_operation_id))
