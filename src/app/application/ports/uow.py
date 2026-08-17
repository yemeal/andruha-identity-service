from types import TracebackType
from typing import Protocol


# Application services own transaction boundaries through this adapter-neutral port.
class AsyncUOWProtocol(Protocol):
    async def __aenter__(self) -> AsyncUOWProtocol: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...
