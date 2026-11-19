import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiokafka.errors import KafkaConnectionError
from faststream.kafka import KafkaBroker

from app.application.outbox.relay import OutboxRelayService
from app.core.settings import Settings
from app.entrypoints.messaging.relay import main


@pytest.mark.asyncio
async def test_relay_recovers_from_temporary_kafka_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUTBOX_BOOTSTRAP_RETRY_INITIAL_SECONDS", "0.001")
    monkeypatch.setenv("OUTBOX_BOOTSTRAP_RETRY_MAX_SECONDS", "0.002")
    settings = Settings()
    broker = MagicMock(spec=KafkaBroker)
    broker.__aenter__ = AsyncMock(
        side_effect=[KafkaConnectionError(), KafkaConnectionError(), broker]
    )
    broker.__aexit__ = AsyncMock()
    relay = MagicMock(spec=OutboxRelayService)
    shutdown = asyncio.Event()

    async def run(*, poll_interval: float, batch_size: int) -> None:
        shutdown.set()

    relay.run = AsyncMock(side_effect=run)
    container = AsyncMock()

    async def get(interface: object) -> object:
        return {
            Settings: settings,
            KafkaBroker: broker,
            OutboxRelayService: relay,
        }[interface]

    container.get = AsyncMock(side_effect=get)

    def setup_signals(event: asyncio.Event, worker: OutboxRelayService) -> None:
        nonlocal shutdown
        shutdown = event

    with (
        patch(
            "app.entrypoints.messaging.relay.create_container", return_value=container
        ),
        patch(
            "app.entrypoints.messaging.relay._setup_signal_handlers",
            side_effect=setup_signals,
        ),
    ):
        await asyncio.wait_for(main(), timeout=1)

    relay.run.assert_awaited_once()
    assert broker.__aenter__.await_count == 3
    container.close.assert_awaited_once()
