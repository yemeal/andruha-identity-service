from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    TimeoutError as PoolTimeoutError,
)

from app.application.exceptions.persistence import (
    PersistenceError,
    PersistenceIntegrityError,
    PersistenceUnavailableError,
    TransactionConflictError,
)


def translate_database_error(error: Exception) -> PersistenceError:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return PersistenceUnavailableError()
    if isinstance(error, DBAPIError):
        sqlstate = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig, "pgcode", None
        )
        if sqlstate in {"40001", "40P01"}:
            return TransactionConflictError()
        if error.connection_invalidated or (
            sqlstate
            and (sqlstate.startswith("08") or sqlstate in {"57P01", "57P02", "57P03"})
        ):
            return PersistenceUnavailableError()
    if isinstance(error, (PoolTimeoutError, DisconnectionError)):
        return PersistenceUnavailableError()
    if isinstance(error, IntegrityError):
        return PersistenceIntegrityError()
    return PersistenceError()
