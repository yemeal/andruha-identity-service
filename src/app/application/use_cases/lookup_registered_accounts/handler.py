from uuid import UUID

from app.application.ports.repositories.users import UserRepositoryProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.use_cases.lookup_registered_accounts.query import (
    LookupRegisteredAccountsQuery,
)


class LookupRegisteredAccountsHandler:
    def __init__(self, users: UserRepositoryProtocol, uow: AsyncUOWProtocol) -> None:
        self._users = users
        self._uow = uow

    async def execute(self, query: LookupRegisteredAccountsQuery) -> tuple[UUID, ...]:
        user_ids = tuple(dict.fromkeys(query.user_ids))
        async with self._uow:
            users = await self._users.get_many(user_ids)
        eligible = {user.id for user in users if user.can_authenticate}
        return tuple(user_id for user_id in user_ids if user_id in eligible)
