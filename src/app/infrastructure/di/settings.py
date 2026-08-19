import dishka
from dishka import Provider, Scope

from app.core.settings import (
    AppSettings,
    IdempotencySettings,
    KafkaSettings,
    OutboxSettings,
    PostgresSettings,
    SecuritySettings,
    Settings,
    ValkeySettings,
    get_settings,
)


class SettingsProvider(Provider):
    scope = Scope.APP

    @dishka.provide
    def settings(self) -> Settings:
        return get_settings()

    @dishka.provide
    def app_settings(self, settings: Settings) -> AppSettings:
        return settings.app

    @dishka.provide
    def postgres_settings(self, settings: Settings) -> PostgresSettings:
        return settings.postgres

    @dishka.provide
    def valkey_settings(self, settings: Settings) -> ValkeySettings:
        return settings.valkey

    @dishka.provide
    def kafka_settings(self, settings: Settings) -> KafkaSettings:
        return settings.kafka

    @dishka.provide
    def outbox_settings(self, settings: Settings) -> OutboxSettings:
        return settings.outbox

    @dishka.provide
    def security_settings(self, settings: Settings) -> SecuritySettings:
        return settings.security

    @dishka.provide
    def idempotency_settings(self, settings: Settings) -> IdempotencySettings:
        return settings.idempotency
