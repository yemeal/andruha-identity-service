from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.application.use_cases.lookup_registered_accounts.handler import (
    LookupRegisteredAccountsHandler,
)
from app.application.use_cases.lookup_registered_accounts.query import (
    LookupRegisteredAccountsQuery,
)
from app.core.settings import SecuritySettings

router = APIRouter(prefix="/internal/v1/accounts", include_in_schema=False)


class AccountsLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_ids: tuple[UUID, ...] = Field(alias="userIds", min_length=1, max_length=100)


class AccountsLookupResponse(BaseModel):
    registered_user_ids: tuple[UUID, ...] = Field(
        serialization_alias="registeredUserIds"
    )


@router.post("/lookup", response_model=AccountsLookupResponse)
@inject
async def lookup_accounts(
    body: AccountsLookupRequest,
    response: Response,
    handler: FromDishka[LookupRegisteredAccountsHandler],
    settings: FromDishka[SecuritySettings],
    service_token: Annotated[str | None, Header(alias="X-Service-Token")] = None,
) -> AccountsLookupResponse:
    configured = settings.IDENTITY_INTERNAL_API_TOKEN
    if configured is None or not configured.get_secret_value().strip():
        raise HTTPException(status_code=503, detail="Service unavailable")
    if service_token is None or not compare_digest(
        service_token.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid service credential")
    user_ids = await handler.execute(
        LookupRegisteredAccountsQuery(user_ids=body.user_ids)
    )
    response.headers["Cache-Control"] = "no-store"
    return AccountsLookupResponse(registered_user_ids=user_ids)
