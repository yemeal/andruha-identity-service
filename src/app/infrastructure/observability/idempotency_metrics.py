from prometheus_client import Counter

_OUTCOMES = Counter(
    "identity_idempotency_outcomes_total",
    "Completed Identity idempotency coordinator outcomes.",
    ("outcome",),
)
_HOT_DEGRADED = Counter(
    "identity_idempotency_hot_degraded_total",
    "Valkey idempotency hot-path degradations that used durable safety.",
    ("stage",),
)


class PrometheusIdempotencyObserver:
    """Low-cardinality metrics adapter; labels are closed coordinator enums."""

    def observe_outcome(self, outcome: str) -> None:
        _OUTCOMES.labels(outcome=outcome).inc()

    def observe_hot_degraded(self, stage: str) -> None:
        _HOT_DEGRADED.labels(stage=stage).inc()
