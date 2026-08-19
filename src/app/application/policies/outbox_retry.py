from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutboxRetryPolicy:
    """
    Прикладная политика экспоненциального бэкоффа со случайным джиттером.

    Защищает брокер и базу от эффекта Thundering Herd (одновременных повторов).
    """

    initial_seconds: float = 1.0
    max_seconds: float = 60.0
    exponent: float = 2.0
    jitter_ratio: float = 0.2
    max_attempts: int = 10

    def __post_init__(self) -> None:
        if self.initial_seconds <= 0:
            raise ValueError("initial_seconds must be positive")
        if self.max_seconds < self.initial_seconds:
            raise ValueError("max_seconds must be at least initial_seconds")
        if self.exponent < 1.0:
            raise ValueError("exponent must be at least 1.0")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0.0 and 1.0")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

    def delay_seconds(self, *, attempt: int, jitter_sample: float) -> float:
        """
        Вычислить задержку для номера попытки с учетом джиттера.

        :param attempt: Номер попытки (1-based).
        :param jitter_sample: Случайное число в диапазоне [-1.0, 1.0].
        :return: Задержка в секундах.
        """
        growth_steps = max(0, attempt - 1)
        capped = min(
            self.max_seconds,
            self.initial_seconds * (self.exponent**growth_steps),
        )
        jittered = capped * (1.0 + self.jitter_ratio * jitter_sample)
        return min(self.max_seconds, max(0.1, jittered))
