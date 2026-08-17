from collections.abc import Awaitable, Callable

import pytest

from app.infrastructure.security.password_hasher import Argon2PasswordHasher


class SyncPasswordHasherStub:
    def __init__(self) -> None:
        self.hash_calls: list[str] = []
        self.verify_calls: list[tuple[str, str]] = []

    def hash(self, password: str) -> str:
        self.hash_calls.append(password)
        return "generated-hash"

    def verify(self, password: str, password_hash: str) -> bool:
        self.verify_calls.append((password, password_hash))
        return password == "correct"


def create_hasher(
    *,
    random_below: Callable[[int], int],
    sleeper: Callable[[float], Awaitable[None]],
) -> tuple[Argon2PasswordHasher, SyncPasswordHasherStub]:
    adapter = Argon2PasswordHasher(
        jitter_min_ms=20,
        jitter_max_ms=80,
        random_below=random_below,
        sleeper=sleeper,
    )
    stub = SyncPasswordHasherStub()
    adapter._hasher = stub  # type: ignore[assignment]
    return adapter, stub


class TestArgon2PasswordHasherConstantWork:
    async def test_missing_hash_runs_dummy_argon2_and_returns_false(self) -> None:
        """
        Проверяем: неизвестный user всё равно оплачивает Argon2-работу.
        Успех: вызывается hash, результат проверки всегда False.
        Нежелательное поведение: None создает быстрый timing-oracle.
        """
        sleeps: list[float] = []

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)

        adapter, stub = create_hasher(
            random_below=lambda upper: upper - 1,
            sleeper=record_sleep,
        )

        result = await adapter.verify_or_dummy("unknown-password", None)

        assert result is False
        assert stub.hash_calls == ["unknown-password"]
        assert stub.verify_calls == []
        assert sleeps == [0.08]

    async def test_real_hash_runs_verify_with_random_jitter(self) -> None:
        """
        Проверяем: обычная проверка получает тот же случайный jitter.
        Успех: задержка лежит в заданном диапазоне, затем вызывается verify.
        Нежелательное поведение: jitter применяется только к dummy-пути.
        """
        sleeps: list[float] = []

        async def record_sleep(delay: float) -> None:
            sleeps.append(delay)

        adapter, stub = create_hasher(
            random_below=lambda _upper: 10,
            sleeper=record_sleep,
        )

        result = await adapter.verify_or_dummy("correct", "stored-hash")

        assert result is True
        assert stub.verify_calls == [("correct", "stored-hash")]
        assert sleeps == [0.03]

    @pytest.mark.parametrize(
        ("max_concurrency", "minimum", "maximum", "message"),
        [
            (2, -1, 10, "password hash jitter"),
            (2, 20, 19, "password hash jitter"),
            (0, 20, 80, "password hash concurrency"),
        ],
    )
    def test_invalid_limits_fail_fast(
        self,
        max_concurrency: int,
        minimum: int,
        maximum: int,
        message: str,
    ) -> None:
        """
        Проверяем: лимиты KDF валидируются при старте.
        Успех: нулевая конкуренция и неверный jitter отклоняются.
        Нежелательное поведение: неверная конфигурация проявляется в запросе.
        """
        with pytest.raises(ValueError, match=message):
            Argon2PasswordHasher(
                max_concurrency=max_concurrency,
                jitter_min_ms=minimum,
                jitter_max_ms=maximum,
            )
