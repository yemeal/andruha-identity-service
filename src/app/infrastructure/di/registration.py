from contextlib import asynccontextmanager

import dishka
from dishka import AsyncContainer, Provider, Scope

from app.application.policies.registration_retry import RegistrationRetryPolicy
from app.application.ports.events import EventPublisherProtocol, UserRegisteredEvent
from app.application.ports.profiles import ProfileProvisionerProtocol
from app.application.ports.registration_recovery import (
    RegistrationObserverProtocol,
    RegistrationScope,
    RegistrationScopeFactory,
)
from app.application.ports.repositories import (
    RegistrationOperationRepositoryProtocol,
    UserRepositoryProtocol,
)
from app.application.ports.security import PasswordHasherProtocol
from app.application.ports.uow import AsyncUOWProtocol
from app.application.services.registration import (
    RegisterUserUseCaseProtocol,
    RegistrationAdministrationProtocol,
    RegistrationAdministrationService,
    RegistrationAttemptProtocol,
    RegistrationCoordinator,
)
from app.application.services.registration_reconciler import (
    RegistrationReconcilerProtocol,
    RegistrationReconcilerService,
)
from app.core.settings import RegistrationSettings
from app.infrastructure.observability.registration_metrics import (
    PrometheusRegistrationObserver,
)


class RegistrationProvider(Provider):
    scope = Scope.APP

    @dishka.provide
    def observer(self) -> RegistrationObserverProtocol:
        return PrometheusRegistrationObserver()

    @dishka.provide
    def scope_factory(self, container: AsyncContainer) -> RegistrationScopeFactory:
        @asynccontextmanager
        async def factory():
            async with container() as request_container:
                yield RegistrationScope(
                    uow=await request_container.get(AsyncUOWProtocol),
                    registrations=await request_container.get(
                        RegistrationOperationRepositoryProtocol
                    ),
                    users=await request_container.get(UserRepositoryProtocol),
                    events=await request_container.get(
                        EventPublisherProtocol[UserRegisteredEvent]
                    ),
                )

        return factory

    @dishka.provide
    def coordinator(
        self,
        scope_factory: RegistrationScopeFactory,
        profile_provisioner: ProfileProvisionerProtocol,
        password_hasher: PasswordHasherProtocol,
        observer: RegistrationObserverProtocol,
        settings: RegistrationSettings,
    ) -> RegistrationCoordinator:
        return RegistrationCoordinator(
            scope_factory=scope_factory,
            profile_provisioner=profile_provisioner,
            password_hasher=password_hasher,
            observer=observer,
            claim_lease_seconds=settings.REGISTRATION_CLAIM_LEASE_SECONDS,
            retry_policy=RegistrationRetryPolicy(
                initial_seconds=settings.REGISTRATION_RETRY_INITIAL_SECONDS,
                max_seconds=settings.REGISTRATION_RETRY_MAX_SECONDS,
                exponent=settings.REGISTRATION_RETRY_EXPONENT,
                jitter_ratio=settings.REGISTRATION_RETRY_JITTER_RATIO,
                max_attempts=settings.REGISTRATION_RETRY_MAX_ATTEMPTS,
            ),
        )

    @dishka.provide
    def register_use_case(
        self, coordinator: RegistrationCoordinator
    ) -> RegisterUserUseCaseProtocol:
        return coordinator

    @dishka.provide
    def registration_attempt(
        self, coordinator: RegistrationCoordinator
    ) -> RegistrationAttemptProtocol:
        return coordinator

    @dishka.provide
    def registration_administration(
        self,
        scope_factory: RegistrationScopeFactory,
    ) -> RegistrationAdministrationProtocol:
        return RegistrationAdministrationService(scope_factory)

    @dishka.provide
    def reconciler(
        self,
        scope_factory: RegistrationScopeFactory,
        attempt: RegistrationAttemptProtocol,
        observer: RegistrationObserverProtocol,
        settings: RegistrationSettings,
    ) -> RegistrationReconcilerProtocol:
        return RegistrationReconcilerService(
            scope_factory=scope_factory,
            attempt=attempt,
            observer=observer,
            claim_lease_seconds=settings.REGISTRATION_CLAIM_LEASE_SECONDS,
        )
