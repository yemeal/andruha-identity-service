import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from faststream.kafka import KafkaBroker

from app.application.services.outbox_relay import OutboxRelayService
from app.core.settings import Settings
from app.entrypoints.messaging.relay import _setup_signal_handlers, main


def test_setup_signal_handlers_triggers_stop_and_sets_event() -> None:
    shutdown_event = asyncio.Event()
    relay_mock = MagicMock(spec=OutboxRelayService)

    captured_handlers: dict[int, object] = {}

    def fake_signal(sig: int, handler: object) -> None:
        captured_handlers[sig] = handler

    with patch("signal.signal", side_effect=fake_signal):
        _setup_signal_handlers(shutdown_event, relay_mock)

    assert signal.SIGINT in captured_handlers
    handler = captured_handlers[signal.SIGINT]
    assert callable(handler)

    # Trigger handler
    handler(signal.SIGINT, None)

    relay_mock.stop.assert_called_once()
    assert shutdown_event.is_set()


@pytest.mark.asyncio
async def test_main_runs_relay_and_shuts_down_cleanly() -> None:
    fake_settings = Settings()
    fake_settings.outbox.OUTBOX_POLL_INTERVAL_SECONDS = 0.01
    fake_settings.outbox.OUTBOX_BATCH_SIZE = 10
    fake_settings.outbox.OUTBOX_SHUTDOWN_TIMEOUT_SECONDS = 1.0

    mock_relay = MagicMock(spec=OutboxRelayService)

    async def fake_run(*, poll_interval: float, batch_size: int) -> None:
        # Runs briefly and finishes
        await asyncio.sleep(0.01)

    mock_relay.run = AsyncMock(side_effect=fake_run)

    mock_broker = MagicMock(spec=KafkaBroker)
    mock_broker.__aenter__ = AsyncMock(return_value=mock_broker)
    mock_broker.__aexit__ = AsyncMock(return_value=None)

    mock_container = AsyncMock()

    async def fake_get(interface: object) -> object:
        if interface is Settings:
            return fake_settings
        if interface is KafkaBroker:
            return mock_broker
        if interface is OutboxRelayService:
            return mock_relay
        raise ValueError(f"Unknown interface: {interface}")

    mock_container.get = AsyncMock(side_effect=fake_get)
    mock_container.close = AsyncMock()

    def fake_setup_signals(
        shutdown_event: asyncio.Event, _relay: OutboxRelayService
    ) -> None:
        # Pre-set shutdown event so main() progresses to graceful shutdown
        shutdown_event.set()

    with (
        patch(
            "app.entrypoints.messaging.relay.create_container",
            return_value=mock_container,
        ),
        patch(
            "app.entrypoints.messaging.relay._setup_signal_handlers",
            side_effect=fake_setup_signals,
        ),
    ):
        await main()

    mock_broker.__aenter__.assert_awaited_once()
    mock_broker.__aexit__.assert_awaited_once()
    mock_relay.run.assert_awaited_once_with(poll_interval=0.01, batch_size=10)
    mock_container.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_handles_shutdown_timeout() -> None:
    fake_settings = Settings()
    fake_settings.outbox.OUTBOX_SHUTDOWN_TIMEOUT_SECONDS = 0.05

    mock_relay = MagicMock(spec=OutboxRelayService)

    async def fake_long_run(*, poll_interval: float, batch_size: int) -> None:
        await asyncio.sleep(10.0)

    mock_relay.run = AsyncMock(side_effect=fake_long_run)

    mock_broker = MagicMock(spec=KafkaBroker)
    mock_broker.__aenter__ = AsyncMock(return_value=mock_broker)
    mock_broker.__aexit__ = AsyncMock(return_value=None)

    mock_container = AsyncMock()

    async def fake_get(interface: object) -> object:
        if interface is Settings:
            return fake_settings
        if interface is KafkaBroker:
            return mock_broker
        if interface is OutboxRelayService:
            return mock_relay
        raise ValueError(f"Unknown interface: {interface}")

    mock_container.get = AsyncMock(side_effect=fake_get)
    mock_container.close = AsyncMock()

    def fake_setup_signals(
        shutdown_event: asyncio.Event, _relay: OutboxRelayService
    ) -> None:
        shutdown_event.set()

    with (
        patch(
            "app.entrypoints.messaging.relay.create_container",
            return_value=mock_container,
        ),
        patch(
            "app.entrypoints.messaging.relay._setup_signal_handlers",
            side_effect=fake_setup_signals,
        ),
    ):
        await main()

    mock_container.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_closes_container_on_exception() -> None:
    mock_container = AsyncMock()
    mock_container.get = AsyncMock(
        side_effect=RuntimeError("Container resolution failed")
    )
    mock_container.close = AsyncMock()

    with (
        patch(
            "app.entrypoints.messaging.relay.create_container",
            return_value=mock_container,
        ),
        pytest.raises(RuntimeError, match="Container resolution failed"),
    ):
        await main()

    mock_container.close.assert_awaited_once()
