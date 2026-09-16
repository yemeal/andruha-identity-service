import pytest
from pydantic import ValidationError

from app.application.policies.registration_retry import RegistrationRetryPolicy
from app.core.settings import RegistrationSettings, Settings


def test_registration_retry_is_exponential_and_capped() -> None:
    policy = RegistrationRetryPolicy(
        initial_seconds=2,
        max_seconds=5,
        exponent=2,
        jitter_ratio=0,
    )

    assert policy.delay_seconds(attempt=1, jitter_sample=0) == 2
    assert policy.delay_seconds(attempt=2, jitter_sample=0) == 4
    assert policy.delay_seconds(attempt=3, jitter_sample=0) == 5


@pytest.mark.parametrize(
    "values",
    [
        {"REGISTRATION_RETRY_INITIAL_SECONDS": 0},
        {"REGISTRATION_RETRY_MAX_SECONDS": 0},
        {
            "REGISTRATION_RETRY_INITIAL_SECONDS": 2,
            "REGISTRATION_RETRY_MAX_SECONDS": 1,
        },
        {"REGISTRATION_RETRY_EXPONENT": 0.5},
        {"REGISTRATION_RETRY_JITTER_RATIO": 1.1},
        {"REGISTRATION_RETRY_MAX_ATTEMPTS": 0},
        {"REGISTRATION_CLAIM_LEASE_SECONDS": 0},
    ],
)
def test_registration_settings_reject_invalid_values(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RegistrationSettings(_env_file=None, **values)


def test_claim_lease_must_cover_bounded_profile_attempt() -> None:
    with pytest.raises(
        ValidationError,
        match="must exceed the maximum bounded Profile attempt window",
    ):
        Settings(
            _env_file=None,
            PROFILE_SERVICE_TIMEOUT_SECONDS=20,
            PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS=1,
            REGISTRATION_CLAIM_LEASE_SECONDS=30,
        )


def test_root_settings_route_registration_configuration() -> None:
    settings = Settings(
        _env_file=None,
        REGISTRATION_POLL_INTERVAL_SECONDS=0.5,
        REGISTRATION_BATCH_SIZE=10,
        REGISTRATION_CLAIM_LEASE_SECONDS=20,
        REGISTRATION_RETRY_INITIAL_SECONDS=0.5,
        REGISTRATION_RETRY_MAX_SECONDS=30,
        REGISTRATION_RETRY_EXPONENT=1.5,
        REGISTRATION_RETRY_JITTER_RATIO=0.1,
        REGISTRATION_RETRY_MAX_ATTEMPTS=7,
        REGISTRATION_SHUTDOWN_TIMEOUT_SECONDS=5,
    )

    assert settings.registration.REGISTRATION_POLL_INTERVAL_SECONDS == 0.5
    assert settings.registration.REGISTRATION_BATCH_SIZE == 10
    assert settings.registration.REGISTRATION_CLAIM_LEASE_SECONDS == 20
    assert settings.registration.REGISTRATION_RETRY_MAX_ATTEMPTS == 7
