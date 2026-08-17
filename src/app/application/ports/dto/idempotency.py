from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.application.value_objects.idempotency import (
    BeginAction,
    ExecutionOutcome,
)


class StoredResult(BaseModel):
    result_type: str = Field(min_length=1, max_length=100)
    result_payload: dict[str, Any] | None = None
    result_version: int = Field(default=1, gt=0)
    resource_type: str | None = Field(default=None, min_length=1, max_length=100)
    resource_id: str | None = None
    resource_version: int | None = Field(default=None, gt=0)

    @field_validator("resource_id", mode="before")
    @classmethod
    def normalize_resource_id(cls, value: object) -> object:
        return str(value) if isinstance(value, UUID) else value

    @model_validator(mode="after")
    def validate_replayability(self) -> StoredResult:
        has_type = self.resource_type is not None
        has_id = self.resource_id is not None
        if has_type != has_id:
            raise ValueError("resource_type and resource_id must be provided together")
        if self.result_payload is None and not has_id:
            raise ValueError("stored result requires payload or resource reference")
        if self.resource_version is not None and not has_id:
            raise ValueError("resource_version requires a resource reference")
        return self


class CompletedIdempotencyResult(StoredResult):
    request_hash: bytes = Field(min_length=32, max_length=32)


class BeginResult(BaseModel):
    action: BeginAction
    completed: CompletedIdempotencyResult | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> BeginResult:
        if self.action is BeginAction.REPLAY and self.completed is None:
            raise ValueError("REPLAY requires a completed result")
        if self.action is not BeginAction.REPLAY and self.completed is not None:
            raise ValueError("only REPLAY may carry a completed result")
        return self


class ExecutionResult(BaseModel):
    outcome: ExecutionOutcome
    completed: CompletedIdempotencyResult | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> ExecutionResult:
        carries_result = self.outcome in {
            ExecutionOutcome.EXECUTED,
            ExecutionOutcome.REPLAY,
        }
        if carries_result != (self.completed is not None):
            raise ValueError("execution outcome and completed result disagree")
        return self
