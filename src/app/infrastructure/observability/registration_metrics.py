from prometheus_client import Counter

_OUTCOMES = Counter(
    "identity_registration_outcomes_total",
    "Durable Identity registration process outcomes.",
    ("outcome",),
)
_RECOVERY_BATCHED = Counter(
    "identity_registration_recovery_claimed_total",
    "Registration operations claimed by recovery workers.",
)


class PrometheusRegistrationObserver:
    def operation_completed(self) -> None:
        _OUTCOMES.labels(outcome="completed").inc()

    def retry_scheduled(self, *, error_class: str) -> None:
        del error_class
        _OUTCOMES.labels(outcome="retry_scheduled").inc()

    def operation_blocked(self, *, error_class: str) -> None:
        del error_class
        _OUTCOMES.labels(outcome="blocked").inc()

    def lost_claim(self) -> None:
        _OUTCOMES.labels(outcome="lost_claim").inc()

    def recovery_batch_claimed(self, *, count: int) -> None:
        _RECOVERY_BATCHED.inc(count)
