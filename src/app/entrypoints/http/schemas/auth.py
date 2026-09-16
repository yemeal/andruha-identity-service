from datetime import datetime
import uuid

from pydantic import Field

from app.domain.aggregates.user import UserRole
from app.domain.value_objects.email import NormalizedEmail
from app.entrypoints.http.schemas.base import CamelCaseBase, CamelCaseOrmBase


class RegisterRequest(CamelCaseBase):
    email: NormalizedEmail
    password: str = Field(min_length=8, max_length=128, repr=False)


class RegisterResponse(CamelCaseBase):
    user_id: uuid.UUID


class PendingRegistrationResponse(CamelCaseBase):
    registration_id: uuid.UUID
    user_id: uuid.UUID
    status: str = "PENDING"


class LoginRequest(CamelCaseBase):
    email: NormalizedEmail
    password: str = Field(min_length=1, max_length=128, repr=False)


class TestLoginResponse(CamelCaseBase):
    access_token: str
    refresh_token: str


class MeResponse(CamelCaseOrmBase):
    id: uuid.UUID
    email: NormalizedEmail
    role: UserRole
    created_at: datetime
