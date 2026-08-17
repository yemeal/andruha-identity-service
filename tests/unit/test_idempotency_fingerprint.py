"""
TDD-контракт канонизации idempotency request.

Fingerprint не зависит от HTTP или Kafka. Транспорт передаёт обычный mapping,
а общий application-компонент связывает ключ с семантическим содержимым операции.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.application.services.idempotency_fingerprint import (
    compute_request_hash,
    hash_idempotency_key,
)


class TestRequestFingerprint:
    def test_mapping_key_order_does_not_change_hash(self) -> None:
        """
        Проверяем: технический порядок JSON-ключей не влияет на request hash.
        Успех: одинаковое содержимое с разным порядком даёт один digest.
        Нежелательное поведение: безопасный retry ошибочно получает payload mismatch.
        """
        first = {
            "currency": "RUB",
            "deliveryAddress": {"city": "Moscow", "street": "Tverskaya"},
        }
        second = {
            "deliveryAddress": {"street": "Tverskaya", "city": "Moscow"},
            "currency": "RUB",
        }

        assert compute_request_hash(first) == compute_request_hash(second)

    def test_order_items_are_order_insensitive(self) -> None:
        """
        Проверяем: перестановка позиций не меняет семантику заказа.
        Успех: два payload с теми же productId и quantity имеют один hash.
        Нежелательное поведение: клиентский retry с иной сортировкой получает 409.
        """
        first = {
            "items": [
                {"productId": "sku-1", "quantity": 2},
                {"productId": "sku-2", "quantity": 1},
            ],
            "currency": "RUB",
        }
        second = {
            "currency": "RUB",
            "items": [
                {"quantity": 1, "productId": "sku-2"},
                {"quantity": 2, "productId": "sku-1"},
            ],
        }

        unordered_paths = {("items",)}

        assert compute_request_hash(
            first,
            unordered_paths=unordered_paths,
        ) == compute_request_hash(
            second,
            unordered_paths=unordered_paths,
        )

    def test_item_quantity_changes_hash(self) -> None:
        """
        Проверяем: бизнес-изменение позиции меняет request hash.
        Успех: иное quantity даёт другой digest при том же idempotency key.
        Нежелательное поведение: под старым ключом возвращается результат другого заказа.
        """
        first = {"items": [{"productId": "sku-1", "quantity": 1}]}
        second = {"items": [{"productId": "sku-1", "quantity": 2}]}

        unordered_paths = {("items",)}

        assert compute_request_hash(
            first,
            unordered_paths=unordered_paths,
        ) != compute_request_hash(
            second,
            unordered_paths=unordered_paths,
        )

    def test_unordered_paths_policy_is_explicit(self) -> None:
        """
        Проверяем: список сортируется только по явной command-specific policy.
        Успех: без unordered_paths перестановка даже поля items меняет request hash.
        Нежелательное поведение: магия по имени поля склеивает разные команды другого проекта.
        """
        first = {
            "items": [
                {"productId": "sku-1", "quantity": 1},
                {"productId": "sku-2", "quantity": 1},
            ]
        }
        second = {
            "items": [
                {"productId": "sku-2", "quantity": 1},
                {"productId": "sku-1", "quantity": 1},
            ]
        }

        assert compute_request_hash(first) != compute_request_hash(second)

    def test_non_string_mapping_key_is_rejected(self) -> None:
        """
        Проверяем: canonical JSON не угадывает строковое представление чужого key type.
        Успех: mapping с integer key отклоняется явной ошибкой.
        Нежелательное поведение: 1 и "1" молча склеиваются в один request hash.
        """
        with pytest.raises((TypeError, ValueError), match="string"):
            compute_request_hash({1: "value"})

    def test_naive_datetime_is_rejected(self) -> None:
        """
        Проверяем: fingerprint не зависит от локального timezone процесса.
        Успех: datetime без offset отклоняется.
        Нежелательное поведение: один command получает разные hashes на разных hosts.
        """
        with pytest.raises((TypeError, ValueError), match="timezone"):
            compute_request_hash({"calculatedAt": datetime(2026, 7, 28, 10, 0, 0)})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_is_rejected(self, value: float) -> None:
        """
        Проверяем: fingerprint использует переносимый JSON number contract.
        Успех: NaN и infinity отклоняются до сериализации.
        Нежелательное поведение: нестандартный JSON digest отличается между runtimes.
        """
        with pytest.raises((TypeError, ValueError), match="finite"):
            compute_request_hash({"value": value})

    def test_equivalent_aware_instants_have_same_hash(self) -> None:
        """
        Проверяем: один момент времени нормализуется в UTC перед hash.
        Успех: 10:00+00 и 13:00+03 дают одинаковый digest.
        Нежелательное поведение: timezone presentation ломает безопасный retry.
        """
        utc_instant = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
        plus_three = utc_instant.astimezone(timezone(timedelta(hours=3)))

        assert compute_request_hash(
            {"calculatedAt": utc_instant}
        ) == compute_request_hash({"calculatedAt": plus_three})


class TestKeyHash:
    def test_raw_idempotency_key_is_not_used_as_persistent_key(self) -> None:
        """
        Проверяем: клиентский Idempotency-Key сохраняется как односторонний digest.
        Успех: результат детерминирован и содержит полный 32-байтный SHA-256 digest.
        Нежелательное поведение: секретоподобный клиентский ключ попадает в БД и логи.
        """
        raw_key = "checkout-session-secret-like-value"

        first = hash_idempotency_key(raw_key)
        second = hash_idempotency_key(raw_key)

        assert first == second
        assert isinstance(first, bytes)
        assert len(first) == 32
        assert first != raw_key
