import json

import pytest

from app.infrastructure.security.replay_result_protector import (
    AESGCMReplayResultProtector,
    load_replay_key,
)


def test_replay_envelope_is_encrypted_and_round_trips() -> None:
    canary = "refresh-canary-value-never-store-plaintext"
    protector = AESGCMReplayResultProtector(
        active_key_id="v2", keys={"v1": b"a" * 32, "v2": b"b" * 32}
    )

    envelope = protector.protect(
        {
            "access_token": "access-canary",
            "refresh_token": canary,
            "token_type": "bearer",
        },
        aad=b"bound-record",
    )

    assert canary not in json.dumps(envelope)
    assert protector.restore(envelope, aad=b"bound-record")["refresh_token"] == canary
    assert envelope["key_id"] == "v2"


def test_replay_envelope_uses_fresh_nonce_and_rejects_other_identity() -> None:
    protector = AESGCMReplayResultProtector(active_key_id="v1", keys={"v1": b"k" * 32})
    first = protector.protect({"refresh_token": "same"}, aad=b"identity-one")
    second = protector.protect({"refresh_token": "same"}, aad=b"identity-one")

    assert first["nonce"] != second["nonce"]
    assert first["ciphertext"] != second["ciphertext"]
    with pytest.raises(ValueError, match="unavailable"):
        protector.restore(first, aad=b"identity-two")


def test_previous_key_remains_decrypt_only() -> None:
    old = AESGCMReplayResultProtector(active_key_id="old", keys={"old": b"o" * 32})
    envelope = old.protect({"refresh_token": "value"}, aad=b"record")
    rotated = AESGCMReplayResultProtector(
        active_key_id="new", keys={"old": b"o" * 32, "new": b"n" * 32}
    )

    assert rotated.restore(envelope, aad=b"record")["refresh_token"] == "value"


def test_raw_key_preserves_trailing_whitespace_byte(tmp_path) -> None:
    key = b"k" * 31 + b"\n"
    key_path = tmp_path / "replay.key"
    key_path.write_bytes(key)

    assert load_replay_key(key_path) == key
