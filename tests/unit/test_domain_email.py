import pytest
from pydantic import TypeAdapter
from app.domain.exceptions import InvalidEmailError

from app.domain.value_objects.email import NormalizedEmail


class TestNormalizedEmail:
    adapter = TypeAdapter(NormalizedEmail)

    def test_normalizes_valid_email(self) -> None:
        """валидный email с пробелами и разным регистром."""
        email = self.adapter.validate_python("  User@EXAMPLE.COM  ")

        assert email == "user@example.com"

    def test_rejects_invalid_email(self) -> None:
        """строку без корректного почтового домена."""
        with pytest.raises(InvalidEmailError):
            self.adapter.validate_python("not-an-email")
