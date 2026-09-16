import hashlib


from app.application.use_cases.refresh.command import RefreshCommand
from app.application.idempotency.fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.use_cases.refresh.handler import (
    PUBLIC_REFRESH_SUBJECT,
    REFRESH_OPERATION,
)


def test_raw_idempotency_key_is_replaced_by_full_digest() -> None:
    raw = "client-key-that-must-not-be-stored"

    digest = hash_idempotency_key(raw)

    assert digest == hashlib.sha256(raw.encode()).digest()
    assert len(digest) == 32
    assert raw.encode() not in digest


def test_refresh_fingerprint_is_semantic_and_stable() -> None:
    first = compute_request_hash({"refresh_token_digest": "abc"})
    second = compute_request_hash({"refresh_token_digest": "abc"})
    different = compute_request_hash({"refresh_token_digest": "def"})

    assert first == second
    assert first != different
    assert len(first) == 32


def test_only_guarded_refresh_use_case_is_public() -> None:
    assert "key_hash" in RefreshCommand.model_fields
    assert PUBLIC_REFRESH_SUBJECT == "public-refresh"
    assert REFRESH_OPERATION == "auth.refresh"
