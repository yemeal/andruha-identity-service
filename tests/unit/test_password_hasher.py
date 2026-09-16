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
        """неизвестный user всё равно оплачивает Argon2-работу."""
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
        """обычная проверка получает тот же случайный jitter."""
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
        """лимиты KDF валидируются при старте."""
        with pytest.raises(ValueError, match=message):
            Argon2PasswordHasher(
                max_concurrency=max_concurrency,
                jitter_min_ms=minimum,
                jitter_max_ms=maximum,
            )


async def test_argon2_failure_uses_application_exception(monkeypatch) -> None:
    from argon2.exceptions import HashingError
    from app.application.exceptions.security import PasswordHashingError

    adapter = Argon2PasswordHasher(jitter_min_ms=0, jitter_max_ms=0)

    def fail(*args, **kwargs):
        raise HashingError("sensitive-input")

    monkeypatch.setattr(adapter._hasher, "hash", fail)
    with pytest.raises(PasswordHashingError) as captured:
        await adapter.hash("password")
    assert "sensitive-input" not in str(captured.value)


async def test_unknown_stored_hash_is_a_service_failure() -> None:
    from app.application.exceptions.security import PasswordHashingError

    adapter = Argon2PasswordHasher(jitter_min_ms=0, jitter_max_ms=0)
    with pytest.raises(PasswordHashingError) as captured:
        await adapter.verify("password", "sensitive-invalid-hash")
    assert "sensitive-invalid-hash" not in str(captured.value)
