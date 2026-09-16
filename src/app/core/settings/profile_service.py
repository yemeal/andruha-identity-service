from typing import Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator

from app.core.settings.base import BaseContextSettings


class ProfileServiceSettings(BaseContextSettings):
    PROFILE_SERVICE_URL: AnyHttpUrl = AnyHttpUrl("http://user-profile-service:8002")
    PROFILE_SERVICE_TOKEN: SecretStr | None = None
    PROFILE_SERVICE_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0, le=30)
    PROFILE_SERVICE_RETRY_DELAY_SECONDS: float = Field(default=0.1, ge=0, le=5)
    PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS: float = Field(default=1.0, gt=0, le=30)
    PROFILE_SERVICE_CB_FAILURES: int = Field(default=5, gt=0, le=100)
    PROFILE_SERVICE_CB_RECOVERY_SECONDS: float = Field(default=30.0, gt=0, le=300)

    @model_validator(mode="after")
    def validate_retry_delays(self) -> Self:
        if (
            self.PROFILE_SERVICE_RETRY_DELAY_SECONDS
            > self.PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS
        ):
            raise ValueError(
                "Profile retry delay must not exceed the maximum retry delay"
            )
        return self

    @field_validator("PROFILE_SERVICE_URL")
    @classmethod
    def validate_origin(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError(
                "Profile service URL must not contain credentials, query or fragment"
            )
        if value.path not in (None, "/"):
            raise ValueError("Profile service URL must be an origin without a path")
        return value

    @field_validator("PROFILE_SERVICE_TOKEN")
    @classmethod
    def validate_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            token = value.get_secret_value()
            if (
                not token
                or not token.isascii()
                or any(char.isspace() for char in token)
            ):
                raise ValueError(
                    "Profile service token must contain non-whitespace ASCII characters"
                )
        return value
