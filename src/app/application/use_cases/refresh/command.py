from pydantic import Field

from app.application.use_cases.base import UseCaseInput


class RefreshCommand(UseCaseInput):
    refresh_token: str = Field(repr=False)
    key_hash: bytes = Field(min_length=32, max_length=32, strict=True, repr=False)
