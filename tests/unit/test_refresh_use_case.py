from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, cast

import pytest

from app.application.exceptions.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyRequestInProgressError,
    RefreshReplayUnavailableError,
)
from app.application.ports.dto.idempotency import (
    CompletedIdempotencyResult,
    ExecutionResult,
)
from app.application.services.auth_service import TokenPair
from app.application.services.idempotency_fingerprint import compute_request_hash
from app.application.services.refresh import (
    PUBLIC_REFRESH_SUBJECT,
    REFRESH_OPERATION,
    REJECTED_RESULT,
    SUCCESS_RESULT,
    RefreshUseCase,
    replay_aad,
)
from app.application.value_objects.idempotency import (
    ExecutionOutcome,
    IdempotencyIdentity,
)
from app.domain.auth_sessions import AuthSession
from app.domain.exceptions import InvalidRefreshTokenError
from app.domain.refresh_tokens import RefreshToken


class FakeCodec:
    def digest(self, value: str) -> bytes:
        return hashlib.sha256(value.encode()).digest()

    def issue(self) -> Any:
        raise AssertionError("replay must not issue tokens")


class FakeCoordinator:
    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.identity: IdempotencyIdentity | None = None
        self.request_hash: bytes | None = None

    async def execute(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        _operation: Any,
        *,
        lease_seconds: int,
        prepare: Any = None,
    ) -> ExecutionResult:
        assert lease_seconds == 30
        assert prepare is None
        self.identity = identity
        self.request_hash = request_hash
        return self.result


class FakeTokenRepository:
    def __init__(self, token: RefreshToken | None) -> None:
        self.token = token

    async def get(self, _entity_id: uuid.UUID) -> RefreshToken | None:
        return self.token


class FakeSessionRepository:
    def __init__(self, session: AuthSession | None) -> None:
        self.session = session

    async def get(self, _entity_id: uuid.UUID) -> AuthSession | None:
        return self.session


class TrackingUOW:
    def __init__(self) -> None:
        self.entries = 0

    async def __aenter__(self) -> TrackingUOW:
        self.entries += 1
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        pass


class FakeProtector:
    def __init__(self, pair: TokenPair, *, fail: bool = False) -> None:
        self.pair = pair
        self.fail = fail
        self.aad: bytes | None = None

    def protect(self, _payload: dict[str, str], *, aad: bytes) -> dict[str, Any]:
        raise AssertionError("replay must not encrypt again")

    def restore(self, _envelope: dict[str, Any], *, aad: bytes) -> dict[str, str]:
        self.aad = aad
        if self.fail:
            raise ValueError("ciphertext rejected")
        return cast(dict[str, str], self.pair.model_dump(mode="python"))


def _fixture(
    outcome: ExecutionOutcome = ExecutionOutcome.REPLAY,
    *,
    result_type: str = SUCCESS_RESULT,
    token_used: bool = False,
    session_active: bool = True,
    protector_fails: bool = False,
) -> tuple[RefreshUseCase, FakeCoordinator, TrackingUOW, FakeProtector]:
    now = datetime.now(UTC)
    session = AuthSession(
        user_id=uuid.uuid4(),
        idle_expires_at=now + timedelta(minutes=5),
        revoked_at=None if session_active else now,
    )
    token = RefreshToken(
        session_id=session.id,
        token_hash=b"t" * 32,
        used_at=now if token_used else None,
    )
    request_hash = compute_request_hash(
        {"refresh_token_digest": FakeCodec().digest("presented").hex()}
    )
    completed = None
    if outcome in {ExecutionOutcome.EXECUTED, ExecutionOutcome.REPLAY}:
        completed = CompletedIdempotencyResult(
            request_hash=request_hash,
            result_type=result_type,
            result_payload={"ciphertext": "opaque-envelope"},
            resource_type="refresh_token" if result_type == SUCCESS_RESULT else None,
            resource_id=token.id if result_type == SUCCESS_RESULT else None,
        )
    coordinator = FakeCoordinator(ExecutionResult(outcome=outcome, completed=completed))
    uow = TrackingUOW()
    protector = FakeProtector(
        TokenPair(access_token="returned-access", refresh_token="returned-refresh"),
        fail=protector_fails,
    )
    use_case = RefreshUseCase(
        coordinator,
        cast(Any, object()),
        cast(Any, FakeTokenRepository(token)),
        cast(Any, FakeSessionRepository(session)),
        FakeCodec(),
        protector,
        uow,
        lease_seconds=30,
    )
    return use_case, coordinator, uow, protector


async def test_active_replay_restores_exact_pair_after_freshness_check() -> None:
    use_case, coordinator, uow, protector = _fixture()

    pair = await use_case.execute(refresh_token="presented", key_hash=b"k" * 32)

    assert pair == TokenPair(
        access_token="returned-access", refresh_token="returned-refresh"
    )
    assert uow.entries == 1
    assert coordinator.identity == IdempotencyIdentity(
        subject_id=PUBLIC_REFRESH_SUBJECT,
        operation=REFRESH_OPERATION,
        key_hash=b"k" * 32,
    )
    assert protector.aad == replay_aad(
        coordinator.identity,
        cast(bytes, coordinator.request_hash),
        SUCCESS_RESULT,
    )


@pytest.mark.parametrize(
    ("token_used", "session_active"),
    [(True, True), (False, False)],
)
async def test_stale_replay_is_rejected_before_decryption(
    token_used: bool, session_active: bool
) -> None:
    use_case, _coordinator, uow, protector = _fixture(
        token_used=token_used, session_active=session_active
    )

    with pytest.raises(InvalidRefreshTokenError):
        await use_case.execute(refresh_token="presented", key_hash=b"k" * 32)

    assert uow.entries == 1
    assert protector.aad is None


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        (ExecutionOutcome.CONFLICT, IdempotencyKeyConflictError),
        (ExecutionOutcome.IN_PROGRESS, IdempotencyRequestInProgressError),
    ],
)
async def test_nonterminal_outcomes_map_to_typed_errors(
    outcome: ExecutionOutcome, expected_error: type[Exception]
) -> None:
    use_case, _coordinator, uow, _protector = _fixture(outcome)

    with pytest.raises(expected_error):
        await use_case.execute(refresh_token="presented", key_hash=b"k" * 32)

    assert uow.entries == 0


async def test_terminal_rejection_replays_as_safe_auth_failure() -> None:
    use_case, _coordinator, uow, _protector = _fixture(result_type=REJECTED_RESULT)

    with pytest.raises(InvalidRefreshTokenError):
        await use_case.execute(refresh_token="presented", key_hash=b"k" * 32)

    assert uow.entries == 0


async def test_undecryptable_envelope_fails_closed() -> None:
    use_case, _coordinator, uow, _protector = _fixture(protector_fails=True)

    with pytest.raises(RefreshReplayUnavailableError):
        await use_case.execute(refresh_token="presented", key_hash=b"k" * 32)

    assert uow.entries == 1
