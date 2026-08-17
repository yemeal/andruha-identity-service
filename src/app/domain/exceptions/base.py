from typing import ClassVar


class DomainError(Exception):
    """Базовый тип ожидаемых ошибок домена."""

    default_message: ClassVar[str] = "Domain error"

    def __init__(self) -> None:
        super().__init__(self.default_message)
