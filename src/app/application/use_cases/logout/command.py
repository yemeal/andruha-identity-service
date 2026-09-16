from pydantic import Field

from app.application.use_cases.base import UseCaseInput


class LogoutCommand(UseCaseInput):
    refresh_token: str | None = Field(default=None, repr=False)
