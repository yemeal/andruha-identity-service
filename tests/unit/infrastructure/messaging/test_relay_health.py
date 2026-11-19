from pathlib import Path
from unittest.mock import patch

import pytest

from app.entrypoints.messaging.health import main, record_state


def test_live_probe_does_not_require_dependencies_while_bootstrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "relay.json"
    monkeypatch.setenv("OUTBOX_HEALTH_STATE_PATH", str(state_path))
    record_state(state_path, ready=False)

    with patch(
        "app.entrypoints.messaging.health.create_container",
        side_effect=AssertionError("liveness must not connect to dependencies"),
    ):
        assert main(["--live"]) == 0
        assert main(["--ready"]) == 1

    state_path.unlink()
    assert main(["--live"]) == 1
