from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import NoReturn, cast
from uuid import UUID

import redis.exceptions
import structlog
from pydantic import TypeAdapter
from redis.asyncio import Redis

from app.application.exceptions.idempotency import (
    IdempotencyStorageUnavailableError,
)
from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
)
from app.application.value_objects.idempotency import BeginAction, IdempotencyIdentity

logger = structlog.get_logger()
_JSON_ADAPTER = TypeAdapter(dict[str, object])


class ValkeyHotIdempotencyStore:
    """Valkey hot lease and replay adapter with owner-token CAS Lua scripts."""

    _BEGIN_SCRIPT = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state then
    if redis.call('EXISTS', KEYS[1]) == 1 then
        return {5}
    end
    redis.call(
        'HSET',
        KEYS[1],
        'format_version', '1',
        'state', 'processing',
        'request_hash', ARGV[1],
        'owner_token', ARGV[2]
    )
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
    return {1}
end
local format_version = redis.call('HGET', KEYS[1], 'format_version')
local current_hash = redis.call('HGET', KEYS[1], 'request_hash')
local owner = redis.call('HGET', KEYS[1], 'owner_token')
local result = redis.call('HGET', KEYS[1], 'result')
if format_version ~= '1' or not current_hash then
    return {5}
end
if state == 'processing' and (not owner or result) then
    return {5}
end
if state == 'completed' and (not result or owner) then
    return {5}
end
if current_hash ~= ARGV[1] then
    return {3}
end
if state == 'completed' then
    return {2, result}
end
if state == 'processing' then
    return {4}
end
return {5}
"""

    _RENEW_SCRIPT = """
local format_version = redis.call('HGET', KEYS[1], 'format_version')
local state = redis.call('HGET', KEYS[1], 'state')
local owner = redis.call('HGET', KEYS[1], 'owner_token')
local request_hash = redis.call('HGET', KEYS[1], 'request_hash')
if format_version == '1' and state == 'processing'
    and owner == ARGV[1] and request_hash then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    return 1
end
return 0
"""

    _COMPLETE_SCRIPT = """
local format_version = redis.call('HGET', KEYS[1], 'format_version')
local state = redis.call('HGET', KEYS[1], 'state')
local owner = redis.call('HGET', KEYS[1], 'owner_token')
local request_hash = redis.call('HGET', KEYS[1], 'request_hash')
if format_version == '1' and state == 'processing'
    and owner == ARGV[1] and request_hash == ARGV[4] then
    redis.call(
        'HSET',
        KEYS[1],
        'state', 'completed',
        'result', ARGV[2]
    )
    redis.call('HDEL', KEYS[1], 'owner_token')
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
    return 1
end
return 0
"""

    _ABANDON_SCRIPT = """
local format_version = redis.call('HGET', KEYS[1], 'format_version')
local state = redis.call('HGET', KEYS[1], 'state')
local owner = redis.call('HGET', KEYS[1], 'owner_token')
if format_version == '1' and state == 'processing' and owner == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        result_ttl_seconds: int,
        key_namespace: str = "andruha-identity-service:idempotency:v1",
    ) -> None:
        if result_ttl_seconds <= 0:
            raise ValueError("result_ttl_seconds must be positive")
        if not key_namespace:
            raise ValueError("key_namespace must not be empty")
        self._redis = redis_client
        self._result_ttl_seconds = result_ttl_seconds
        self._key_namespace = key_namespace

    async def begin(
        self,
        identity: IdempotencyIdentity,
        request_hash: bytes,
        owner_token: UUID,
        lease_seconds: int,
    ) -> BeginResult:
        if len(request_hash) != 32:
            raise ValueError("request_hash must be a full SHA-256 digest")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        raw: object
        try:
            raw = cast(
                object,
                await self._redis.eval(
                    self._BEGIN_SCRIPT,
                    1,
                    self._storage_key(identity),
                    request_hash.hex(),
                    str(owner_token),
                    str(lease_seconds),
                ),
            )
        except redis.exceptions.RedisError as error:
            self._raise_unavailable("begin", error)

        try:
            code = self._result_code(raw)
            if not isinstance(raw, (list, tuple)):
                raise RuntimeError(
                    "Valkey idempotency script returned malformed result"
                )
            values = cast(Sequence[object], raw)
            if code == 1:
                return BeginResult(action=BeginAction.ACQUIRED)
            if code == 2:
                if len(values) < 2 or values[1] is None:
                    raise RuntimeError("completed entry has no result")
                completed = self._decode_completed(values[1])
                if completed.request_hash != request_hash:
                    raise RuntimeError("completed result hash does not match entry")
                return BeginResult(
                    action=BeginAction.REPLAY,
                    completed=completed,
                )
            if code == 3:
                return BeginResult(action=BeginAction.CONFLICT)
            if code == 4:
                return BeginResult(action=BeginAction.IN_PROGRESS)
            raise RuntimeError(f"idempotency entry has unknown state code {code}")
        except RuntimeError as error:
            self._raise_corrupted(error)

    async def renew(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
        lease_seconds: int,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        try:
            result = await self._redis.eval(
                self._RENEW_SCRIPT,
                1,
                self._storage_key(identity),
                str(owner_token),
                str(lease_seconds),
            )
            return bool(result)
        except redis.exceptions.RedisError as error:
            self._raise_unavailable("renew", error)

    async def complete(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
        result: CompletedIdempotencyResult,
    ) -> bool:
        encoded = self._encode_completed(result)
        try:
            completed = await self._redis.eval(
                self._COMPLETE_SCRIPT,
                1,
                self._storage_key(identity),
                str(owner_token),
                encoded,
                str(self._result_ttl_seconds),
                result.request_hash.hex(),
            )
            return bool(completed)
        except redis.exceptions.RedisError as error:
            self._raise_unavailable("complete", error)

    async def abandon(
        self,
        identity: IdempotencyIdentity,
        owner_token: UUID,
    ) -> bool:
        try:
            abandoned = await self._redis.eval(
                self._ABANDON_SCRIPT,
                1,
                self._storage_key(identity),
                str(owner_token),
            )
            return bool(abandoned)
        except redis.exceptions.RedisError as error:
            self._raise_unavailable("abandon", error)

    def _storage_key(self, identity: IdempotencyIdentity) -> str:
        material = (
            identity.subject_id.encode("utf-8")
            + b"\0"
            + identity.operation.encode("utf-8")
            + b"\0"
            + identity.key_hash
        )
        digest = hashlib.sha256(material).hexdigest()
        return f"{self._key_namespace}:{digest}"

    @staticmethod
    def _result_code(raw: object) -> int:
        if not isinstance(raw, (list, tuple)) or not raw:
            raise RuntimeError("Valkey idempotency script returned malformed result")
        values = cast(Sequence[object], raw)
        value = values[0]
        if not isinstance(value, (str, bytes, int)):
            raise RuntimeError("Valkey idempotency script returned malformed result")
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Valkey idempotency script returned malformed result"
            ) from error

    @staticmethod
    def _encode_completed(result: CompletedIdempotencyResult) -> str:
        payload = cast(
            dict[str, object],
            result.model_dump(mode="json", exclude={"request_hash"}),
        )
        payload["request_hash"] = result.request_hash.hex()
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_completed(raw: object) -> CompletedIdempotencyResult:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                raise TypeError("completed result is not JSON text")
            payload = _JSON_ADAPTER.validate_json(raw)
            request_hash = payload.get("request_hash")
            if not isinstance(request_hash, str):
                raise TypeError("completed result request hash is not text")
            payload["request_hash"] = bytes.fromhex(request_hash)
            return CompletedIdempotencyResult.model_validate(payload)
        except (KeyError, TypeError, UnicodeError, ValueError) as error:
            raise RuntimeError("Valkey completed result is malformed") from error

    @staticmethod
    def _raise_unavailable(operation: str, error: Exception) -> NoReturn:
        logger.warning(
            "idempotency_valkey_unavailable",
            operation=operation,
            error_type=type(error).__name__,
        )
        raise IdempotencyStorageUnavailableError() from error

    @staticmethod
    def _raise_corrupted(error: Exception) -> NoReturn:
        logger.error(
            "idempotency_valkey_corrupted",
            error_type=type(error).__name__,
        )
        raise IdempotencyStorageUnavailableError() from error
