from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import json
from typing import Protocol
from uuid import UUID

from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    RefreshReplayUnavailableError,
)
from app.application.ports.dto import AccessPrincipal
from app.application.ports.dto.idempotency import (
    StoredResult,
)
from app.application.ports.idempotency import (
    IdempotencyCoordinatorProtocol,
    ReplayResultProtectorProtocol,
)
from app.application.ports.repositories import (
    AuthSessionRepositoryProtocol,
    RefreshTokenRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    OpaqueRefreshTokenCodecProtocol,
)
from app.application.ports.uow import AsyncUOWProtocol
from app.application.services.auth_service import TokenPair
from app.application.services.idempotency_fingerprint import compute_request_hash
from app.application.value_objects.idempotency import (
    ExecutionOutcome,
    IdempotencyIdentity,
)
from app.domain.base import utc_now
from app.domain.exceptions import DomainErrors, InvalidRefreshTokenError
from app.domain.refresh_tokens import RefreshToken

PUBLIC_REFRESH_SUBJECT = "public-refresh"
REFRESH_OPERATION = "auth.refresh"
SUCCESS_RESULT = "auth.refresh.success"
REJECTED_RESULT = "auth.refresh.rejected"


class TransactionalRefreshOperationProtocol(Protocol):
    async def execute(
        self, raw_token: str, identity: IdempotencyIdentity, request_hash: bytes
    ) -> StoredResult: ...


class RefreshUseCaseProtocol(Protocol):
    async def execute(self, *, refresh_token: str, key_hash: bytes) -> TokenPair: ...


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


class TransactionalRefreshOperation(TransactionalRefreshOperationProtocol):
    """Refresh business effect. The caller owns the already-open SQL transaction."""

    def __init__(
        self,
        users: UserRepositoryProtocol,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        issuer: AccessTokenIssuerProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        session_idle_ttl: timedelta | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._users, self._tokens, self._sessions = users, tokens, sessions
        self._issuer, self._codec, self._protector = issuer, codec, protector
        self._session_idle_ttl = (
            session_idle_ttl if session_idle_ttl is not None else timedelta(days=30)
        )
        self._clock = clock

    async def execute(
        self, raw_token: str, identity: IdempotencyIdentity, request_hash: bytes
    ) -> StoredResult:
        token = await self._tokens.get_by_hash_for_update(self._codec.digest(raw_token))
        if token is None:
            raise DomainErrors.Token.INVALID_REFRESH()
        session = await self._sessions.get_for_update(token.session_id)
        if session is None:
            raise DomainErrors.Token.INVALID_REFRESH()
        now = self._clock()
        if token.is_used:
            session.revoke(now)
            await self._sessions.update(session)
            return StoredResult(
                result_type=REJECTED_RESULT, result_payload={"code": "invalid_refresh"}
            )
        if not session.is_active(now):
            raise DomainErrors.Session.INACTIVE()
        user = await self._users.get(session.user_id)
        if user is None or not user.can_authenticate:
            session.revoke(now)
            await self._sessions.update(session)
            return StoredResult(
                result_type=REJECTED_RESULT, result_payload={"code": "invalid_refresh"}
            )
        access_token = self._issuer.issue(
            AccessPrincipal(user_id=user.id, role=user.role), now=now
        )
        issued_refresh = self._codec.issue()
        token.consume(now)
        await self._tokens.update(token)
        session.extend_idle(now, self._session_idle_ttl)
        await self._sessions.update(session)
        replacement = await self._tokens.create(
            RefreshToken(session_id=session.id, token_hash=issued_refresh.digest)
        )
        pair = TokenPair(access_token=access_token, refresh_token=issued_refresh.value)
        envelope = self._protector.protect(
            pair.model_dump(mode="json"),
            aad=replay_aad(identity, request_hash, SUCCESS_RESULT),
        )
        return StoredResult(
            result_type=SUCCESS_RESULT,
            result_payload=envelope,
            resource_type="refresh_token",
            resource_id=str(replacement.id),
        )


class RefreshUseCase(RefreshUseCaseProtocol):
    def __init__(
        self,
        coordinator: IdempotencyCoordinatorProtocol,
        operation: TransactionalRefreshOperationProtocol,
        tokens: RefreshTokenRepositoryProtocol,
        sessions: AuthSessionRepositoryProtocol,
        codec: OpaqueRefreshTokenCodecProtocol,
        protector: ReplayResultProtectorProtocol,
        uow: AsyncUOWProtocol,
        lease_seconds: int = 30,
    ) -> None:
        self._coordinator, self._operation = coordinator, operation
        self._tokens, self._sessions, self._codec = tokens, sessions, codec
        self._protector, self._uow = protector, uow
        self._lease_seconds = lease_seconds

    async def execute(self, *, refresh_token: str, key_hash: bytes) -> TokenPair:
        identity = IdempotencyIdentity(
            subject_id=PUBLIC_REFRESH_SUBJECT,
            operation=REFRESH_OPERATION,
            key_hash=key_hash,
        )
        request_hash = compute_request_hash(
            {"refresh_token_digest": self._codec.digest(refresh_token).hex()}
        )

        async def effect() -> StoredResult:
            return await self._operation.execute(refresh_token, identity, request_hash)

        result = await self._coordinator.execute(
            identity,
            request_hash,
            effect,
            lease_seconds=self._lease_seconds,
        )
        if result.outcome is ExecutionOutcome.CONFLICT:
            raise IdempotencyKeyConflictError()
        if result.outcome is ExecutionOutcome.IN_PROGRESS:
            raise IdempotencyRequestInProgressError()
        completed = result.completed
        if completed is None:
            raise RuntimeError("successful idempotency outcome has no result")
        if completed.result_type == REJECTED_RESULT:
            raise DomainErrors.Token.INVALID_REFRESH()
        if (
            completed.result_type != SUCCESS_RESULT
            or completed.result_payload is None
            or completed.resource_id is None
        ):
            raise RefreshReplayUnavailableError()
        try:
            async with self._uow:
                token = await self._tokens.get(UUID(str(completed.resource_id)))
                if token is None or token.is_used:
                    raise InvalidRefreshTokenError()
                session = await self._sessions.get(token.session_id)
                if session is None or not session.is_active(utc_now()):
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
        except InvalidRefreshTokenError:
            raise
        except Exception as error:
            raise RefreshReplayUnavailableError() from error
