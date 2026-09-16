from pydantic import Field

from app.application.use_cases.base import UseCaseInput


class GetCurrentUserQuery(UseCaseInput):
    access_token: str = Field(repr=False)
