from datetime import datetime
from typing import Protocol

from app.application.ports.dto.security import AccessPrincipal


class AccessTokenIssuerProtocol(Protocol):
    """
    Выпускает короткоживущий stateless access JWT.

    Токен не содержит sid и не проверяется по состоянию AuthSession. После
    logout он остается действителен до exp, без denylist и запросов в БД.
    """

    def issue(
        self,
        principal: AccessPrincipal,
        now: datetime,
    ) -> str: ...
