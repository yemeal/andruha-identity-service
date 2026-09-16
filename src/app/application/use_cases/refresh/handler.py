from collections.abc import Callable
from datetime import datetime, timedelta
import json
from typing import Protocol
from uuid import UUID

from app.application.dto.token_pair import TokenPair
from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    RefreshReplayUnavailableError,
)
from app.application.idempotency.fingerprint import compute_request_hash
from app.application.ports.dto.idempotency import StoredResult
from app.application.ports.idempotency import (
    IdempotencyCoordinatorProtocol,
    ReplayResultProtectorProtocol,
)
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import OpaqueRefreshTokenCodecProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.tokens import TokenPairIssuer
from app.application.use_cases.refresh.command import RefreshCommand
from app.application.value_objects.idempotency import (
    ExecutionOutcome,
    IdempotencyIdentity,
)
from app.domain.base import utc_now
from app.domain.exceptions import InvalidRefreshTokenError

PUBLIC_REFRESH_SUBJECT = "public-refresh"
REFRESH_OPERATION = "auth.refresh"
SUCCESS_RESULT = "auth.refresh.success"
REJECTED_RESULT = "auth.refresh.rejected"


class TransactionalRefreshOperationProtocol(Protocol):
    async def execute(
        self, raw_token: str, identity: IdempotencyIdentity, request_hash: bytes
    ) -> StoredResult: ...


def replay_aad(
    identity: IdempotencyIdentity,
    request_hash: bytes,
    result_type: str,
    result_version: int = 1,
) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "subject_id": identity.subject_id,
            "operation": identity.operation,
            "key_hash": identity.key_hash.hex(),
            "request_hash": request_hash.hex(),
            "result_type": result_type,
            "result_version": result_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class TransactionalRefreshOperation:
    """Сохраняет переход агрегата и зашифрованный результат внутри открытой UoW."""

    def __init__(
        self,
        users: UserRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        issuer: TokenPairIssuer,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        session_idle_ttl: timedelta,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._issuer = issuer
        self._codec = codec
        self._protector = protector
        self._session_idle_ttl = session_idle_ttl
        self._clock = clock

    async def execute(
        self, raw_token: str, identity: IdempotencyIdentity, request_hash: bytes
    ) -> StoredResult:
        session = await self._sessions.get_by_refresh_hash_for_update(
            self._codec.digest(raw_token)
        )
        if session is None:
            raise InvalidRefreshTokenError()
        user = await self._users.get(session.user_id)
        now = self._clock()
        if not session.accepts_refresh(
            now=now, user_can_authenticate=user is not None and user.can_authenticate
        ):
            await self._sessions.save(session)
            # Отказ является результатом: исключение откатило бы отзыв family.
            return StoredResult(
                result_type=REJECTED_RESULT, result_payload={"code": "invalid_refresh"}
            )
        if user is None:
            raise InvalidRefreshTokenError()
        issued = self._issuer.issue(user, now)
        replacement = session.rotate(
            now=now, idle_ttl=self._session_idle_ttl, token_hash=issued.refresh_digest
        )
        await self._sessions.save(session)
        envelope = self._protector.protect(
            issued.pair.model_dump(mode="json"),
            aad=replay_aad(identity, request_hash, SUCCESS_RESULT),
        )
        return StoredResult(
            result_type=SUCCESS_RESULT,
            result_payload=envelope,
            resource_type="refresh_token",
            resource_id=str(replacement.id),
        )


class RefreshHandler:
    def __init__(
        self,
        coordinator: IdempotencyCoordinatorProtocol,
        operation: TransactionalRefreshOperationProtocol,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        uow: AsyncUOWProtocol,
        lease_seconds: int = 30,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._coordinator = coordinator
        self._operation = operation
        self._sessions = sessions
        self._codec = codec
        self._protector = protector
        self._uow = uow
        self._lease_seconds = lease_seconds
        self._clock = clock

    async def execute(self, command: RefreshCommand) -> TokenPair:
        identity = IdempotencyIdentity(
            subject_id=PUBLIC_REFRESH_SUBJECT,
            operation=REFRESH_OPERATION,
            key_hash=command.key_hash,
        )
        request_hash = compute_request_hash(
            {"refresh_token_digest": self._codec.digest(command.refresh_token).hex()}
        )

        async def effect() -> StoredResult:
            return await self._operation.execute(
                command.refresh_token, identity, request_hash
            )

        result = await self._coordinator.execute(
            identity, request_hash, effect, lease_seconds=self._lease_seconds
        )
        if result.outcome is ExecutionOutcome.CONFLICT:
            raise IdempotencyKeyConflictError()
        if result.outcome is ExecutionOutcome.IN_PROGRESS:
            raise IdempotencyRequestInProgressError()
        completed = result.completed
        if completed is None:
            raise RuntimeError("successful idempotency outcome has no result")
        if completed.result_type == REJECTED_RESULT:
            raise InvalidRefreshTokenError()
        if (
            completed.result_type != SUCCESS_RESULT
            or completed.result_payload is None
            or completed.resource_id is None
        ):
            raise RefreshReplayUnavailableError()
        try:
            token_id = UUID(str(completed.resource_id))
            async with self._uow:
                session = await self._sessions.get_by_refresh_id(token_id)
                if session is None or not session.can_replay(self._clock()):
                    raise InvalidRefreshTokenError()
            payload = self._protector.restore(
                completed.result_payload,
                aad=replay_aad(
                    identity,
                    request_hash,
                    completed.result_type,
                    completed.result_version,
                ),
            )
            return TokenPair.model_validate(payload)
        except ValueError:
            raise RefreshReplayUnavailableError() from None
