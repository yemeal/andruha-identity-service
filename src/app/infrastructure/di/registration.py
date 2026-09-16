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
from app.application.registration.reconciler import (
    RegistrationReconcilerProtocol,
    RegistrationReconcilerService,
)
from app.application.use_cases.redrive_registration.handler import (
    RedriveRegistrationHandler,
)
from app.application.use_cases.register.handler import RegisterUserHandler
from app.application.use_cases.resume_registration.handler import (
    ResumeRegistrationHandler,
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
    def resume_handler(
        self,
        scope_factory: RegistrationScopeFactory,
        profile_provisioner: ProfileProvisionerProtocol,
        observer: RegistrationObserverProtocol,
        settings: RegistrationSettings,
    ) -> ResumeRegistrationHandler:
        return ResumeRegistrationHandler(
            scope_factory=scope_factory,
            profile_provisioner=profile_provisioner,
            observer=observer,
            retry_policy=RegistrationRetryPolicy(
                initial_seconds=settings.REGISTRATION_RETRY_INITIAL_SECONDS,
                max_seconds=settings.REGISTRATION_RETRY_MAX_SECONDS,
                exponent=settings.REGISTRATION_RETRY_EXPONENT,
                jitter_ratio=settings.REGISTRATION_RETRY_JITTER_RATIO,
                max_attempts=settings.REGISTRATION_RETRY_MAX_ATTEMPTS,
            ),
        )

    @dishka.provide
    def register_handler(
        self,
        scope_factory: RegistrationScopeFactory,
        password_hasher: PasswordHasherProtocol,
        resume: ResumeRegistrationHandler,
        settings: RegistrationSettings,
    ) -> RegisterUserHandler:
        return RegisterUserHandler(
            scope_factory=scope_factory,
            password_hasher=password_hasher,
            resume=resume,
            claim_lease_seconds=settings.REGISTRATION_CLAIM_LEASE_SECONDS,
        )

    @dishka.provide
    def registration_administration(
        self,
        scope_factory: RegistrationScopeFactory,
    ) -> RedriveRegistrationHandler:
        return RedriveRegistrationHandler(scope_factory)

    @dishka.provide
    def reconciler(
        self,
        scope_factory: RegistrationScopeFactory,
        attempt: ResumeRegistrationHandler,
        observer: RegistrationObserverProtocol,
        settings: RegistrationSettings,
    ) -> RegistrationReconcilerProtocol:
        return RegistrationReconcilerService(
            scope_factory=scope_factory,
            attempt=attempt,
            observer=observer,
            claim_lease_seconds=settings.REGISTRATION_CLAIM_LEASE_SECONDS,
        )
