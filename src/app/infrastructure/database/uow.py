from types import TracebackType

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

# Протокол перенесён в application/ports/uow.py (порт принадлежит application-слою);
# здесь остаётся только конкретная SQLAlchemy-реализация (адаптер).

logger = structlog.get_logger()


class SQLAlchemyAsyncUOW:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> SQLAlchemyAsyncUOW:
        logger.debug("uow_enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        except Exception:
            await self.session.rollback()
            raise
        finally:
            logger.debug("uow_exit")
