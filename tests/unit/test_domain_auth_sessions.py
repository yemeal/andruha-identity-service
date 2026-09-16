from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.aggregates.auth_session import AuthSession
from app.domain.exceptions import (
    AuthSessionInactiveError,
    InvalidDomainStateError,
    InvalidSessionLifetimeError,
    InvalidTimestampError,
)
from app.domain.aggregates.user import User

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def session() -> AuthSession:
    return AuthSession.start(
        user_id=uuid4(), token_hash=b"a" * 32, now=NOW, idle_ttl=timedelta(days=1)
    )


def test_rotation_consumes_token_and_extends_same_session() -> None:
    root = session()
    root_id = root.id
    token_id = root.refresh_token.id
    later = NOW + timedelta(hours=1)

    replacement = root.rotate(
        now=later, idle_ttl=timedelta(days=30), token_hash=b"b" * 32
    )

    assert root.id == root_id
    assert root.refresh_token.id == token_id
    assert root.refresh_token.used_at == later
    assert replacement.session_id == root.id
    assert replacement.used_at is None
    assert root.idle_expires_at == later + timedelta(days=30)


@pytest.mark.parametrize("revoked", [False, True])
def test_inactive_session_cannot_rotate(revoked: bool) -> None:
    root = session()
    if revoked:
        root.revoke(NOW)
    before = root.model_copy(deep=True)
    with pytest.raises(AuthSessionInactiveError):
        root.rotate(
            now=NOW + timedelta(days=2),
            idle_ttl=timedelta(days=30),
            token_hash=b"b" * 32,
        )
    assert root == before


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_nonpositive_ttl_leaves_aggregate_unchanged(ttl: timedelta) -> None:
    root = session()
    before = root.model_copy(deep=True)
    with pytest.raises(InvalidSessionLifetimeError):
        root.rotate(now=NOW, idle_ttl=ttl, token_hash=b"b" * 32)
    assert root == before


@pytest.mark.parametrize("digest", [b"short", b"a" * 32])
def test_invalid_replacement_does_not_consume_original(digest: bytes) -> None:
    root = session()
    before = root.model_copy(deep=True)
    with pytest.raises(InvalidDomainStateError):
        root.rotate(now=NOW, idle_ttl=timedelta(days=1), token_hash=digest)
    assert root == before
    assert root.refresh_token.used_at is None


def test_reuse_revokes_session_through_root() -> None:
    root = session()
    root.rotate(now=NOW, idle_ttl=timedelta(days=1), token_hash=b"b" * 32)
    assert root.accepts_refresh(now=NOW, user_can_authenticate=True) is False
    assert root.revoked_at == NOW


def test_entities_reject_direct_mutation() -> None:
    root = session()
    with pytest.raises(InvalidDomainStateError):
        root.revoked_at = NOW
    with pytest.raises(InvalidDomainStateError):
        root.refresh_token.used_at = NOW
    user = User(email="User@EXAMPLE.COM", password_hash="hash", created_at=NOW)
    with pytest.raises(InvalidDomainStateError):
        user.password_hash = ""
    assert user.email == "user@example.com"


def test_naive_time_is_a_domain_error_and_does_not_mutate() -> None:
    root = session()
    with pytest.raises(InvalidTimestampError):
        root.revoke(NOW.replace(tzinfo=None))
    assert root.revoked_at is None


def test_invalid_user_copy_does_not_bypass_validation() -> None:
    user = User(email="user@example.com", password_hash="hash", created_at=NOW)
    with pytest.raises(InvalidDomainStateError):
        user.model_copy(update={"password_hash": ""})
    assert user.password_hash == "hash"


def test_business_change_updates_explicit_field_serialization() -> None:
    user = User(email="user@example.com", password_hash="hash", created_at=NOW)
    user.disable(NOW)
    state = user.model_dump(exclude_unset=True)
    assert state["status"] == "DISABLED"
    assert state["updated_at"] == NOW
