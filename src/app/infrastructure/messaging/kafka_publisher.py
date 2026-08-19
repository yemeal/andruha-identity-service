from faststream.exceptions import FastStreamException
from faststream.kafka import KafkaBroker
import structlog

from app.application.exceptions import (
    PermanentPublishError,
    TransientPublishError,
)
from app.application.ports.dto.outbox import OutboxMessage

logger = structlog.get_logger(__name__)


class FastStreamKafkaPublisher:
    """Адаптер публикации Outbox-сообщений в Kafka через FastStream."""

    def __init__(self, broker: KafkaBroker) -> None:
        self._broker = broker

    async def publish(self, message: OutboxMessage) -> None:
        log = logger.bind(
            topic=message.topic,
            message_id=str(message.id),
            event_type=message.type,
        )

        try:
            await self._broker.publish(
                message=message.payload,
                topic=message.topic,
                key=message.key.encode("utf-8"),
                headers={
                    "event_id": str(message.id),
                    "event_type": message.type,
                },
            )
            log.debug("outbox_message_published_to_kafka")

        except TimeoutError as error:
            log.warning(
                "kafka_publish_timeout",
                error_type=type(error).__name__,
            )
            raise TransientPublishError(
                f"Publish timeout for topic {message.topic}"
            ) from error

        except ConnectionError as error:
            log.warning(
                "kafka_connection_error",
                error_type=type(error).__name__,
            )
            raise TransientPublishError(
                f"Connection error publishing to topic {message.topic}"
            ) from error

        except ValueError as error:
            log.error(
                "kafka_invalid_payload_error",
                error=str(error),
            )
            raise PermanentPublishError(
                f"Invalid payload for message {message.id}: {error}"
            ) from error

        except FastStreamException as error:
            log.warning(
                "kafka_broker_exception",
                error_type=type(error).__name__,
            )
            raise TransientPublishError(
                f"Broker error publishing to topic {message.topic}: {error}"
            ) from error

        except Exception as error:
            log.exception(
                "kafka_unexpected_publish_failure",
                error_type=type(error).__name__,
            )
            raise TransientPublishError(
                f"Unexpected error publishing to topic {message.topic}: {type(error).__name__}"
            ) from error
