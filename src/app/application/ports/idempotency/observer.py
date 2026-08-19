from typing import Protocol


class IdempotencyObserverProtocol(Protocol):
    def observe_outcome(self, outcome: str) -> None: ...

    def observe_hot_degraded(self, stage: str) -> None: ...
