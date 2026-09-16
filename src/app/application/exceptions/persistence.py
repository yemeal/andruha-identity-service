class PersistenceError(Exception):
    """Ошибка хранения с безопасным сообщением."""

    def __init__(self) -> None:
        super().__init__("Persistence operation failed")


class PersistenceUnavailableError(PersistenceError):
    """Хранилище недоступно; исход commit может быть неизвестен."""


class TransactionConflictError(PersistenceError):
    """Транзакция отклонена из-за конкурентного изменения."""


class PersistenceIntegrityError(PersistenceError):
    """Данные нарушают ограничения хранилища."""


class StoredStateError(PersistenceError):
    """Сохранённые данные не соответствуют модели."""


class EntityNotFoundError(PersistenceError):
    """Обновляемый агрегат больше не существует."""
