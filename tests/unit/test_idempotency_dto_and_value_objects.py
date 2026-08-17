"""
TDD contract for idempotency DTOs and application value objects.

Невозможные combinations отбрасываются при создании модели, а не проверяются
разными entrypoints по-разному.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.application.ports.dto.idempotency import (
    BeginResult,
    CompletedIdempotencyResult,
    ExecutionResult,
    StoredResult,
)
from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)
from app.application.value_objects.idempotency import (
    BeginAction,
    ExecutionOutcome,
    IdempotencyIdentity,
)


def _stored() -> StoredResult:
    resource_id = uuid.uuid4()
    return StoredResult(
        result_type="generic_snapshot",
        result_payload={"id": str(resource_id)},
        result_version=1,
        resource_type="generic_resource",
        resource_id=resource_id,
        resource_version=1,
    )


def _completed() -> CompletedIdempotencyResult:
    return CompletedIdempotencyResult(
        request_hash=compute_request_hash({"version": 1}),
        **_stored().model_dump(),
    )


class TestDecisionInvariants:
    @pytest.mark.parametrize(
        "factory",
        [
            lambda: BeginResult(action=BeginAction.REPLAY),
            lambda: ExecutionResult(outcome=ExecutionOutcome.REPLAY),
            lambda: ExecutionResult(outcome=ExecutionOutcome.EXECUTED),
        ],
    )
    def test_success_or_replay_requires_completed_result(self, factory) -> None:
        """
        Проверяем: решение с готовым эффектом всегда несёт generic result.
        Успех: модель отклоняет REPLAY или EXECUTED без completed payload.
        Нежелательное поведение: entrypoint получает success, который нечего вернуть.
        """
        with pytest.raises(ValidationError):
            factory()

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: BeginResult(
                action=BeginAction.CONFLICT,
                completed=_completed(),
            ),
            lambda: BeginResult(
                action=BeginAction.IN_PROGRESS,
                completed=_completed(),
            ),
            lambda: ExecutionResult(
                outcome=ExecutionOutcome.CONFLICT,
                completed=_completed(),
            ),
            lambda: ExecutionResult(
                outcome=ExecutionOutcome.IN_PROGRESS,
                completed=_completed(),
            ),
        ],
    )
    def test_non_result_decision_forbids_completed_payload(self, factory) -> None:
        """
        Проверяем: conflict и in-progress не маскируются сохранённым success.
        Успех: inconsistent combination отклоняется моделью.
        Нежелательное поведение: разные entrypoints выбирают разные ветки одного result.
        """
        with pytest.raises(ValidationError):
            factory()


class TestDigestAndResultInvariants:
    def test_resource_id_is_normalized_for_port_serialization(self) -> None:
        resource_id = uuid.uuid4()

        stored = StoredResult(
            result_type="generic_snapshot",
            resource_type="generic_resource",
            resource_id=resource_id,
        )

        assert stored.resource_id == str(resource_id)

    def test_identity_requires_full_sha256_key_hash(self) -> None:
        """
        Проверяем: storage identity не принимает сырой или усечённый key digest.
        Успех: 32 байта валидны, 31 байт отклоняется.
        Нежелательное поведение: collision risk отличается между Redis и PostgreSQL.
        """
        valid = IdempotencyIdentity(
            subject_id=str(uuid.uuid4()),
            operation="create_order",
            key_hash=hash_idempotency_key("client-key"),
        )

        assert len(valid.key_hash) == 32
        with pytest.raises(ValidationError):
            IdempotencyIdentity(
                subject_id=str(uuid.uuid4()),
                operation="create_order",
                key_hash=b"x" * 31,
            )

    def test_completed_result_requires_full_request_hash(self) -> None:
        """
        Проверяем: payload mismatch guard всегда сравнивает полный SHA-256.
        Успех: completed result с усечённым request hash отклоняется.
        Нежелательное поведение: durable fallback принимает слабый digest.
        """
        with pytest.raises(ValidationError):
            CompletedIdempotencyResult(
                request_hash=b"x" * 31,
                **_stored().model_dump(),
            )

    @pytest.mark.parametrize("version_field", ["result_version", "resource_version"])
    def test_result_versions_are_positive(self, version_field: str) -> None:
        """
        Проверяем: replay version нельзя перепутать с отсутствующей или initial zero.
        Успех: нулевая result/resource version отклоняется.
        Нежелательное поведение: ETag fallback возвращает несуществующую версию ресурса.
        """
        data = _stored().model_dump()
        data[version_field] = 0

        with pytest.raises(ValidationError):
            StoredResult.model_validate(data)
