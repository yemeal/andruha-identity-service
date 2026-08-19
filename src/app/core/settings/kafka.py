from pydantic import field_validator

from app.core.settings.base import BaseContextSettings


class KafkaSettings(BaseContextSettings):
    KAFKA_BOOTSTRAP_SERVERS: str = "kafka:29092"
    KAFKA_CLIENT_ID: str = "andruha-identity-service"
    KAFKA_USER_REGISTERED_TOPIC: str = "identity.user-registered.v1"
    KAFKA_GROUP_ID: str = "andruha-identity-service"
    KAFKA_AUTO_OFFSET_RESET: str = "earliest"

    @property
    def bootstrap_servers(self) -> str:
        return self.KAFKA_BOOTSTRAP_SERVERS

    @field_validator("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_CLIENT_ID")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Kafka configuration fields must not be blank")
        return value
