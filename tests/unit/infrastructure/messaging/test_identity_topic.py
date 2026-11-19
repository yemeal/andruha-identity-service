import pytest

from app.core.settings import KafkaSettings


def test_default_registration_topic_matches_identity_event_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KAFKA_USER_REGISTERED_TOPIC", raising=False)
    assert (
        KafkaSettings(_env_file=None).KAFKA_USER_REGISTERED_TOPIC
        == "identity.events.v1"
    )
