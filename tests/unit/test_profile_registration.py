import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError

from app.application.exceptions.profiles import (
    ProfileProvisioningRejectedError,
    ProfileProvisioningUnavailableError,
)
from app.core.settings import Settings
from app.core.settings.profile_service import ProfileServiceSettings
from app.infrastructure.http.profile_provisioner import (
    HTTPProfileProvisioner,
    ProfileCircuitBreaker,
)


def profile_provisioner(
    client: httpx2.AsyncClient,
    *,
    fail_max: int = 5,
    recovery_timeout: float = 30,
) -> HTTPProfileProvisioner:
    return HTTPProfileProvisioner(
        client,
        configured=True,
        circuit_breaker=ProfileCircuitBreaker(
            fail_max=fail_max,
            recovery_timeout=recovery_timeout,
        ),
        retry_delay_seconds=0,
    )


@pytest.mark.parametrize("first_status", [204, 423, 429, 500, 502, 503, 504, None])
async def test_profile_client_retries_identical_request(
    first_status: int | None,
) -> None:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            if first_status is None:
                raise httpx2.ReadTimeout("response lost", request=request)
            return httpx2.Response(first_status)
        return httpx2.Response(204)

    user_id = uuid4()
    registered_at = datetime(2026, 9, 11, tzinfo=UTC)
    async with httpx2.AsyncClient(
        base_url="http://profile.test",
        headers={"X-Service-Token": "test-secret"},
        transport=httpx2.MockTransport(respond),
    ) as client:
        await profile_provisioner(client).create_profile(user_id, registered_at)
    assert len(requests) == (1 if first_status == 204 else 2)
    assert requests[0].method == "PUT"
    assert requests[0].url.path == f"/internal/v1/profiles/{user_id}"
    assert requests[0].headers["X-Service-Token"] == "test-secret"
    assert b"2026-09-11T00:00:00+00:00" in requests[0].content
    assert all(request.content == requests[0].content for request in requests)


@pytest.mark.parametrize("status", [200, 201, 202, 301, 401, 403, 404, 422, 503, None])
async def test_profile_client_rejects_unconfirmed_creation(status: int | None) -> None:
    attempts = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if status is None:
            raise httpx2.ConnectError("unavailable", request=request)
        return httpx2.Response(status)

    async with httpx2.AsyncClient(
        base_url="http://profile.test", transport=httpx2.MockTransport(respond)
    ) as client:
        with pytest.raises(ProfileProvisioningUnavailableError):
            await profile_provisioner(client).create_profile(uuid4(), datetime.now(UTC))
    assert attempts == (2 if status in (503, None) else 1)


async def test_profile_circuit_opens_and_fails_fast() -> None:
    requests = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(503)

    breaker = ProfileCircuitBreaker(fail_max=2, recovery_timeout=60)
    async with httpx2.AsyncClient(
        base_url="http://profile.test", transport=httpx2.MockTransport(respond)
    ) as client:
        provisioner = HTTPProfileProvisioner(
            client,
            configured=True,
            circuit_breaker=breaker,
            retry_delay_seconds=0,
        )
        with pytest.raises(ProfileProvisioningUnavailableError):
            await provisioner.create_profile(uuid4(), datetime.now(UTC))
        with pytest.raises(ProfileProvisioningUnavailableError):
            await provisioner.create_profile(uuid4(), datetime.now(UTC))

    assert requests == 2


async def test_profile_rejection_is_not_retried_or_counted_as_outage() -> None:
    requests = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(409)

    async with httpx2.AsyncClient(
        base_url="http://profile.test", transport=httpx2.MockTransport(respond)
    ) as client:
        provisioner = profile_provisioner(client, fail_max=1)
        for _ in range(2):
            with pytest.raises(ProfileProvisioningRejectedError):
                await provisioner.create_profile(uuid4(), datetime.now(UTC))

    assert requests == 2


async def test_profile_retry_after_is_bounded() -> None:
    requests = 0
    delays: list[float] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx2.Response(429, headers={"Retry-After": "10"})
        return httpx2.Response(204)

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    async with httpx2.AsyncClient(
        base_url="http://profile.test", transport=httpx2.MockTransport(respond)
    ) as client:
        await HTTPProfileProvisioner(
            client,
            configured=True,
            circuit_breaker=ProfileCircuitBreaker(fail_max=5, recovery_timeout=30),
            retry_delay_seconds=0.1,
            retry_max_delay_seconds=1,
            sleeper=record_delay,
        ).create_profile(uuid4(), datetime.now(UTC))

    assert requests == 2
    assert delays == [1]


async def test_profile_circuit_recovers_through_one_probe() -> None:
    statuses = iter((503, 503, 204, 204))
    requests = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(next(statuses))

    async with httpx2.AsyncClient(
        base_url="http://profile.test", transport=httpx2.MockTransport(respond)
    ) as client:
        provisioner = profile_provisioner(client, fail_max=2, recovery_timeout=0.01)
        with pytest.raises(ProfileProvisioningUnavailableError):
            await provisioner.create_profile(uuid4(), datetime.now(UTC))
        with pytest.raises(ProfileProvisioningUnavailableError):
            await provisioner.create_profile(uuid4(), datetime.now(UTC))
        assert requests == 2

        await asyncio.sleep(0.02)
        await provisioner.create_profile(uuid4(), datetime.now(UTC))
        await provisioner.create_profile(uuid4(), datetime.now(UTC))

    assert requests == 4


async def test_missing_configuration_does_not_send_request() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("unconfigured client sent a request")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        with pytest.raises(ProfileProvisioningUnavailableError):
            await HTTPProfileProvisioner(
                client,
                configured=False,
                circuit_breaker=ProfileCircuitBreaker(fail_max=1, recovery_timeout=30),
                retry_delay_seconds=0,
            ).create_profile(uuid4(), datetime.now(UTC))


@pytest.mark.parametrize(
    "values",
    [
        {"PROFILE_SERVICE_URL": "ftp://profile.test"},
        {"PROFILE_SERVICE_URL": "http://user:password@profile.test"},
        {"PROFILE_SERVICE_URL": "http://profile.test/path"},
        {"PROFILE_SERVICE_URL": "http://profile.test?secret=1"},
        {"PROFILE_SERVICE_TIMEOUT_SECONDS": 0},
        {"PROFILE_SERVICE_RETRY_DELAY_SECONDS": -1},
        {"PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS": 0},
        {
            "PROFILE_SERVICE_RETRY_DELAY_SECONDS": 2,
            "PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS": 1,
        },
        {"PROFILE_SERVICE_CB_FAILURES": 0},
        {"PROFILE_SERVICE_CB_RECOVERY_SECONDS": 0},
        {"PROFILE_SERVICE_TOKEN": ""},
        {"PROFILE_SERVICE_TOKEN": "secret\r\nheader"},
    ],
)
def test_profile_settings_reject_invalid_configuration(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ProfileServiceSettings(_env_file=None, **values)


def test_root_settings_route_profile_configuration() -> None:
    settings = Settings(
        _env_file=None,
        PROFILE_SERVICE_URL="https://profile.example.com",
        PROFILE_SERVICE_TOKEN="test-secret",
        PROFILE_SERVICE_TIMEOUT_SECONDS=1,
        PROFILE_SERVICE_RETRY_DELAY_SECONDS=0.2,
        PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS=1.5,
        PROFILE_SERVICE_CB_FAILURES=4,
        PROFILE_SERVICE_CB_RECOVERY_SECONDS=20,
    )
    assert (
        str(settings.profile_service.PROFILE_SERVICE_URL)
        == "https://profile.example.com/"
    )
    assert (
        settings.profile_service.PROFILE_SERVICE_TOKEN.get_secret_value()
        == "test-secret"
    )
    assert settings.profile_service.PROFILE_SERVICE_TIMEOUT_SECONDS == 1
    assert settings.profile_service.PROFILE_SERVICE_RETRY_DELAY_SECONDS == 0.2
    assert settings.profile_service.PROFILE_SERVICE_RETRY_MAX_DELAY_SECONDS == 1.5
    assert settings.profile_service.PROFILE_SERVICE_CB_FAILURES == 4
    assert settings.profile_service.PROFILE_SERVICE_CB_RECOVERY_SECONDS == 20
