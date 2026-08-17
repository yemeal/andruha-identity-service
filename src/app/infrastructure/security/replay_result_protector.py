import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import TypeAdapter, ValidationError

_PAYLOAD_ADAPTER = TypeAdapter(dict[str, str])


class AESGCMReplayResultProtector:
    def __init__(self, *, active_key_id: str, keys: dict[str, bytes]) -> None:
        if active_key_id not in keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError("replay encryption key ring is invalid")
        self._active_key_id = active_key_id
        self._keys = keys

    def protect(self, payload: dict[str, str], *, aad: bytes) -> dict[str, Any]:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(
            nonce, plaintext, aad
        )
        return {
            "version": 1,
            "algorithm": "AES-256-GCM",
            "key_id": self._active_key_id,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def restore(self, envelope: dict[str, Any], *, aad: bytes) -> dict[str, str]:
        try:
            if (
                envelope.get("version") != 1
                or envelope.get("algorithm") != "AES-256-GCM"
            ):
                raise ValueError("unsupported replay envelope")
            key_id = str(envelope["key_id"])
            key = self._keys[key_id]
            nonce = base64.b64decode(str(envelope["nonce"]), validate=True)
            ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
            return _PAYLOAD_ADAPTER.validate_json(plaintext)
        except (InvalidTag, KeyError, TypeError, ValidationError, ValueError) as error:
            raise ValueError("refresh replay result unavailable") from error


def load_replay_key(path: Path) -> bytes:
    raw = path.read_bytes()
    if len(raw) == 32:
        return raw
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except ValueError as error:
        raise ValueError("replay encryption key file is invalid") from error
    if len(decoded) != 32:
        raise ValueError("replay encryption key must contain 32 bytes")
    return decoded
