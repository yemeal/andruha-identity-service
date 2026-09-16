from types import TracebackType
from typing import Protocol


class AsyncUOWProtocol(Protocol):
    """Commit on success, roll back on failure; storage failures raise PersistenceError."""

    async def __aenter__(self) -> AsyncUOWProtocol: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...
