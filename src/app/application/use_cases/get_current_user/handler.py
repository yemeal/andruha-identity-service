from app.application.dto.current_user import CurrentUser
from app.application.exceptions.security import InvalidTokenError
from app.application.ports.repositories import UserRepositoryProtocol
from app.application.ports.security import AccessTokenVerifierProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.use_cases.get_current_user.query import GetCurrentUserQuery


class GetCurrentUserHandler:
    def __init__(
        self,
        users: UserRepositoryProtocol,
        verifier: AccessTokenVerifierProtocol,
        uow: AsyncUOWProtocol,
    ) -> None:
        self._users = users
        self._verifier = verifier
        self._uow = uow

    async def execute(self, query: GetCurrentUserQuery) -> CurrentUser:
        claims = self._verifier.verify(query.access_token)
        async with self._uow:
            user = await self._users.get(claims.user_id)
            if user is None or not user.can_authenticate:
                raise InvalidTokenError()
        return CurrentUser.model_validate(user)
