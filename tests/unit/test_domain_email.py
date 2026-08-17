import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.value_objects.email import NormalizedEmail


class TestNormalizedEmail:
    adapter = TypeAdapter(NormalizedEmail)

    def test_normalizes_valid_email(self) -> None:
        """
        Проверяем: валидный email с пробелами и разным регистром.
        Успех: адрес сохранён в единой регистронезависимой форме.
        Нежелательное поведение: одинаковые адреса получают разные ключи поиска.
        """
        email = self.adapter.validate_python("  User@EXAMPLE.COM  ")

        assert email == "user@example.com"

    def test_rejects_invalid_email(self) -> None:
        """
        Проверяем: строку без корректного почтового домена.
        Успех: доменный тип отклоняет значение.
        Нежелательное поведение: невалидный адрес попадает в доменную модель.
        """
        with pytest.raises(ValidationError):
            self.adapter.validate_python("not-an-email")
