import pytest

from app.core.settings import _read_bool, _read_mute_loggers, _read_port, get_settings


@pytest.mark.parametrize("raw_value", ["1", "true", "TRUE", " yes ", "on"])
def test_read_bool_accepts_true_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    monkeypatch.setenv("FEATURE", raw_value)

    assert _read_bool("FEATURE", False) is True


@pytest.mark.parametrize("raw_value", ["0", "false", "FALSE", " no ", "off"])
def test_read_bool_accepts_false_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    monkeypatch.setenv("FEATURE", raw_value)

    assert _read_bool("FEATURE", True) is False


def test_read_bool_uses_default_when_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEATURE", raising=False)

    assert _read_bool("FEATURE", True) is True


def test_read_bool_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE", "sometimes")

    with pytest.raises(ValueError, match="FEATURE must be a boolean value"):
        _read_bool("FEATURE", False)


@pytest.mark.parametrize("port", [1, 8001, 65535])
def test_read_port_accepts_valid_range(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
) -> None:
    monkeypatch.setenv("PORT", str(port))

    assert _read_port(9000) == port


@pytest.mark.parametrize("port", [0, 65536])
def test_read_port_rejects_out_of_range_value(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
) -> None:
    monkeypatch.setenv("PORT", str(port))

    with pytest.raises(ValueError, match="PORT must be between 1 and 65535"):
        _read_port(9000)


def test_read_port_rejects_non_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORT", "http")

    with pytest.raises(ValueError):
        _read_port(9000)


def test_read_mute_loggers_trims_and_drops_empty_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUTE_LOGGERS", " uvicorn.access, ,httpx ")

    assert _read_mute_loggers() == ("uvicorn.access", "httpx")


def test_get_settings_reads_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "SERVICE_NAME": "test-service",
        "APP_VERSION": "9.9.9",
        "APP_ENVIRONMENT": "test",
        "HOST": "127.0.0.1",
        "PORT": "9123",
        "DEV_LOGS": "false",
        "LOG_LEVEL": "debug",
        "MUTE_LOGGERS": "httpx,uvicorn.access",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = get_settings()

    assert settings.SERVICE_NAME == "test-service"
    assert settings.APP_VERSION == "9.9.9"
    assert settings.APP_ENVIRONMENT == "test"
    assert settings.HOST == "127.0.0.1"
    assert settings.PORT == 9123
    assert settings.DEV_LOGS is False
    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.MUTE_LOGGERS == ("httpx", "uvicorn.access")
