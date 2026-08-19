class OutboxError(Exception):
    """Базовое исключение для ошибок подсистемы Outbox."""


class PublishError(OutboxError):
    """Базовое исключение для ошибок публикации сообщений в брокер."""


class TransientPublishError(PublishError):
    """
    Временная (retryable) ошибка публикации в брокер.

    Возникает при временной недоступности брокера, сетевых таймаутах,
    перевыборах лидера партиции Kafka и т.д.
    Сообщение возвращается в очередь со сдвигом available_at (экспоненциальный бэкофф).
    """


class PermanentPublishError(PublishError):
    """
    Неисправимая (non-retryable) ошибка публикации в брокер.

    Возникает при дефектах схемы, превышении максимального размера сообщения,
    невалидных заголовках, poison pill или недопустимом payload.
    Сообщение немедленно переводится в статус QUARANTINED.
    """


class RetryExhaustedError(PermanentPublishError):
    """
    Исчерпан лимит попыток доставки сообщения (Retry Limit Exceeded).

    Возникает, когда сообщение превысило максимальное число попыток ретрая
    (MAX_ATTEMPTS) и принудительно переводится в QUARANTINED.
    """
