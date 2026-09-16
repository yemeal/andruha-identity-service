from unittest.mock import AsyncMock
import asyncio

import pytest
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    ProgrammingError,
    TimeoutError as PoolTimeoutError,
)

from app.application.exceptions.persistence import (
    PersistenceError,
    PersistenceIntegrityError,
    PersistenceUnavailableError,
    TransactionConflictError,
)
from app.domain.exceptions import InvalidCredentialsError
from app.infrastructure.database.uow import SQLAlchemyAsyncUOW


class DriverError(Exception):
    def __init__(self, state: str) -> None:
        self.sqlstate = state
        super().__init__("sensitive-driver-message")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            IntegrityError("secret SQL", {"password": "secret"}, DriverError("23505")),
            PersistenceIntegrityError,
        ),
        (DBAPIError("SQL", {}, DriverError("40001")), TransactionConflictError),
        (DBAPIError("SQL", {}, DriverError("40P01")), TransactionConflictError),
        (DBAPIError("SQL", {}, DriverError("08006")), PersistenceUnavailableError),
        (PoolTimeoutError("secret"), PersistenceUnavailableError),
        (ConnectionRefusedError("secret"), PersistenceUnavailableError),
        (ProgrammingError("SQL", {}, DriverError("42601")), PersistenceError),
    ],
)
@pytest.mark.parametrize("phase", ["body", "commit"])
async def test_transaction_translates_database_errors(
    error, expected, phase: str
) -> None:
    session = AsyncMock()
    if phase == "commit":
        session.commit.side_effect = error
    with pytest.raises(expected) as captured:
        async with SQLAlchemyAsyncUOW(session):
            if phase == "body":
                raise error
    assert type(captured.value) is expected
    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is error
    session.rollback.assert_awaited()


@pytest.mark.parametrize("error", [InvalidCredentialsError(), asyncio.CancelledError()])
async def test_business_error_and_cancellation_survive_rollback(
    error: BaseException,
) -> None:
    session = AsyncMock()
    with pytest.raises(type(error)) as captured:
        async with SQLAlchemyAsyncUOW(session):
            raise error
    assert captured.value is error
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
