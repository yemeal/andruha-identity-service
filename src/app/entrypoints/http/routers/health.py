import asyncio

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter(prefix="/health", tags=["health"])


async def check_postgres(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def check_valkey(client: Redis) -> bool:
    try:
        return bool(
            await client.ping()  # pyright: ignore[reportUnknownMemberType]
        )
    except Exception:
        return False


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


# TODO (Issue 9): Устранить утечку инфраструктурных драйверов в Presentation Layer.
# Сейчас HTTP-роутер напрямую зависит от драйверов (AsyncEngine, Redis) и сам
# выполняет низкоуровневые запросы ('SELECT 1' и 'client.ping()').
# Рекомендуемый рефакторинг:
# 1. Создать порт в application/ports/health.py:
#    class HealthCheckProtocol(Protocol):
#        async def check_readiness(self) -> ReadinessStatus: ...
# 2. Создать адаптер в infrastructure/observability/health_checker.py.
# 3. Зарегистрировать провайдер HealthCheckProvider в DI (Dishka).
# 4. Инжектить FromDishka[HealthCheckProtocol] в эндпоинт /health/ready вместо AsyncEngine и Redis.
@router.get("/ready")
@inject
async def ready(
    response: Response, engine: FromDishka[AsyncEngine], client: FromDishka[Redis]
) -> dict[str, str]:
    postgres_ok, valkey_ok = await asyncio.gather(
        check_postgres(engine), check_valkey(client)
    )
    if not postgres_ok:
        response.status_code = 503
    return {
        "status": "ready" if postgres_ok else "unavailable",
        "postgres": "ok" if postgres_ok else "unavailable",
        "valkey": "ok" if valkey_ok else "degraded",
    }
