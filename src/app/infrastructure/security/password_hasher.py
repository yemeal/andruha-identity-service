import asyncio
from collections.abc import Awaitable, Callable
from secrets import randbelow

from pwdlib import PasswordHash


class Argon2PasswordHasher:
    """
    Адаптер PasswordHasherProtocol на pwdlib (argon2id).

    Argon2 выполняется в отдельном потоке и ограничивается семафором. Перед
    каждой операцией добавляется небольшой криптографически случайный jitter.
    Он только усложняет единичные timing-измерения; основную защиту дает
    verify_or_dummy, который выполняет одинаково дорогую KDF и без user hash.
    """

    def __init__(
        self,
        max_concurrency: int = 2,
        *,
        jitter_min_ms: int = 20,
        jitter_max_ms: int = 80,
        random_below: Callable[[int], int] = randbelow,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("password hash concurrency must be positive")
        if jitter_min_ms < 0 or jitter_max_ms < jitter_min_ms:
            raise ValueError("invalid password hash jitter range")

        # recommended() selects the maintained Argon2id parameters.
        self._hasher = PasswordHash.recommended()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._jitter_min_ms = jitter_min_ms
        self._jitter_max_ms = jitter_max_ms
        self._random_below = random_below
        self._sleeper = sleeper

    async def _sleep_jitter(self) -> None:
        window_ms = self._jitter_max_ms - self._jitter_min_ms + 1
        delay_ms = self._jitter_min_ms + self._random_below(window_ms)
        if delay_ms > 0:
            # Задержка не занимает дефицитный слот Argon2-семафора.
            await self._sleeper(delay_ms / 1000)

    async def hash(self, password: str) -> str:
        await self._sleep_jitter()
        async with self._semaphore:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password: str, password_hash: str) -> bool:
        await self._sleep_jitter()
        async with self._semaphore:
            return await asyncio.to_thread(
                self._hasher.verify,
                password,
                password_hash,
            )

    async def verify_or_dummy(
        self,
        password: str,
        password_hash: str | None,
    ) -> bool:
        if password_hash is None:
            # Hash и verify используют одни recommended Argon2id-параметры.
            # Полученный hash намеренно отбрасывается: это только constant-work.
            await self.hash(password)
            return False

        return await self.verify(password, password_hash)
