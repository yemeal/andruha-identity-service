from uuid import UUID

from app.application.ports.dto.registration import RegistrationOperation
from app.application.use_cases.base import UseCaseInput


class ResumeRegistrationCommand(UseCaseInput):
    operation: RegistrationOperation
    owner_token: UUID
