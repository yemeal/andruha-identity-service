from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from httpx2 import AsyncClient

from app.application.ports.profiles import ProfileProvisionerProtocol
from app.core.settings.profile_service import ProfileServiceSettings
from app.infrastructure.http.profile_provisioner import (
    HTTPProfileProvisioner,
    ProfileCircuitBreaker,
)


class ProfilesProvider(Provider):
    scope = Scope.APP

    @provide
    def profile_circuit_breaker(
        self, settings: ProfileServiceSettings
    ) -> ProfileCircuitBreaker:
        return ProfileCircuitBreaker(
            fail_max=settings.PROFILE_SERVICE_CB_FAILURES,
            recovery_timeout=settings.PROFILE_SERVICE_CB_RECOVERY_SECONDS,
        )

    @provide
    async def profile_provisioner(
        self,
        settings: ProfileServiceSettings,
        circuit_breaker: ProfileCircuitBreaker,
    ) -> AsyncIterator[ProfileProvisionerProtocol]:
        token = settings.PROFILE_SERVICE_TOKEN
        headers = {"X-Service-Token": token.get_secret_value()} if token else {}
        async with AsyncClient(
            base_url=str(settings.PROFILE_SERVICE_URL),
            headers=headers,
            timeout=settings.PROFILE_SERVICE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            yield HTTPProfileProvisioner(
                client,
                configured=token is not None,
                circuit_breaker=circuit_breaker,
                retry_delay_seconds=settings.PROFILE_SERVICE_RETRY_DELAY_SECONDS,
                retry_max_delay_seconds=(
                    settings.PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS
                ),
            )
