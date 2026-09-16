from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel

JsonPath = tuple[str, ...]


def hash_idempotency_key(raw_key: str) -> bytes:
    """Hash a client key before it crosses a persistence port."""
    if not raw_key:
        raise ValueError("idempotency key must not be empty")
    return hashlib.sha256(raw_key.encode("utf-8")).digest()


def compute_request_hash(
    payload: Mapping[str, Any] | BaseModel,
    *,
    unordered_paths: Set[JsonPath] | None = None,
) -> bytes:
    """
    Строит SHA-256 из семантического application payload.

    Нейтральное ядро не знает, какие списки являются множествами. Order use case
    явно передаёт {("lines",)}, остальные последовательности сохраняют порядок.
    """
    effective_paths = unordered_paths if unordered_paths is not None else frozenset()
    raw = (
        payload.model_dump(mode="python") if isinstance(payload, BaseModel) else payload
    )
    canonical = _canonicalize(raw, path=(), unordered_paths=effective_paths)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _canonicalize(
    value: Any,
    *,
    path: JsonPath,
    unordered_paths: Set[JsonPath],
) -> Any:
    if isinstance(value, BaseModel):
        return _canonicalize(
            value.model_dump(mode="python"),
            path=path,
            unordered_paths=unordered_paths,
        )
    if isinstance(value, Mapping):
        raw_mapping = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in raw_mapping):
            raise TypeError("fingerprint mappings require string keys")
        mapping = cast(Mapping[str, object], value)
        return {
            key: _canonicalize(
                item,
                path=(*path, key),
                unordered_paths=unordered_paths,
            )
            for key, item in sorted(mapping.items())
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        items = [
            _canonicalize(
                item,
                path=(*path, "[]"),
                unordered_paths=unordered_paths,
            )
            for item in sequence
        ]
        if path in unordered_paths:
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        return items
    if isinstance(value, Set):
        value_set = cast(Set[object], value)
        items = [
            _canonicalize(item, path=(*path, "{}"), unordered_paths=unordered_paths)
            for item in value_set
        ]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("fingerprint decimals must be finite")
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("fingerprint datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _canonicalize(
            value.value,
            path=path,
            unordered_paths=unordered_paths,
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("fingerprint floats must be finite")
        return value if value else 0.0
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported fingerprint value: {type(value).__name__}")
