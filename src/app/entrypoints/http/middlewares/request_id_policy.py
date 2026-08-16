"""Policy for resolving a safe request correlation ID."""

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Protocol
import uuid


RequestIdGenerator = Callable[[], str]

_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


def _generate_request_id() -> str:
    return str(uuid.uuid4())


class RequestIdResolver(Protocol):
    def resolve(self, candidate: str | None) -> str:
        """Return a safe propagated ID or generate a new one."""
        ...


@dataclass(frozen=True, slots=True)
class ValidatedRequestIdResolver:
    max_length: int = 128
    generator: RequestIdGenerator = _generate_request_id

    def __post_init__(self) -> None:
        if self.max_length < 1:
            raise ValueError("max_length must be positive")

    def resolve(self, candidate: str | None) -> str:
        if candidate is not None and self._is_valid(candidate):
            return candidate

        generated = self.generator()
        if not self._is_valid(generated):
            raise ValueError("request id generator returned an unsafe value")
        return generated

    def _is_valid(self, value: str) -> bool:
        return (
            len(value) <= self.max_length
            and _SAFE_REQUEST_ID.fullmatch(value) is not None
        )
