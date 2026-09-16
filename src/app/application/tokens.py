from datetime import datetime

from app.application.dto.token_pair import IssuedTokenPair, TokenPair
from app.application.ports.dto import AccessPrincipal
from app.application.ports.security import (
    AccessTokenIssuerProtocol,
    OpaqueRefreshTokenCodecProtocol,
)
from app.domain.aggregates.user import User


class TokenPairIssuer:
    def __init__(
        self,
        access: AccessTokenIssuerProtocol,
        refresh: OpaqueRefreshTokenCodecProtocol,
    ) -> None:
        self._access = access
        self._refresh = refresh

    def issue(self, user: User, now: datetime) -> IssuedTokenPair:
        access = self._access.issue(
            AccessPrincipal(user_id=user.id, role=user.role), now=now
        )
        refresh = self._refresh.issue()
        return IssuedTokenPair(
            pair=TokenPair(access_token=access, refresh_token=refresh.value),
            refresh_digest=refresh.digest,
        )
