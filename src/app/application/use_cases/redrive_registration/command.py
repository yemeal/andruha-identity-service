from uuid import UUID

from app.application.use_cases.base import UseCaseInput


class RedriveRegistrationCommand(UseCaseInput):
    operation_id: UUID
