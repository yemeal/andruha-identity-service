from contextlib import suppress
from types import TracebackType

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database.exceptions import translate_database_error


class SQLAlchemyAsyncUOW:
    """Commit atomically and translate database failures at the transaction boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> SQLAlchemyAsyncUOW:
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
        except (SQLAlchemyError, ConnectionError, TimeoutError) as error:
            with suppress(SQLAlchemyError, ConnectionError, TimeoutError):
                await self.session.rollback()
            raise translate_database_error(error) from error
        except BaseException:
            await self.session.rollback()
            raise
        if isinstance(exc_val, (SQLAlchemyError, ConnectionError, TimeoutError)):
            raise translate_database_error(exc_val) from exc_val
