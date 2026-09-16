from pathlib import Path


SOURCE_ROOT = Path(__file__).parents[2] / "src" / "app"


def test_registration_components_follow_hexagonal_ownership() -> None:
    expected = {
        "application/ports/dto/registration.py",
        "application/ports/profiles.py",
        "application/ports/registration_recovery.py",
        "application/ports/repositories/registration_operations.py",
        "application/services/registration.py",
        "application/services/registration_reconciler.py",
        "infrastructure/database/models/registration_operations.py",
        "infrastructure/database/repositories/registration_operation_repository.py",
        "infrastructure/http/profile_provisioner.py",
    }

    assert all((SOURCE_ROOT / path).is_file() for path in expected)


def test_registration_application_has_no_transport_or_persistence_imports() -> None:
    application_files = (
        SOURCE_ROOT / "application" / "services" / "registration.py",
        SOURCE_ROOT / "application" / "services" / "registration_reconciler.py",
    )
    forbidden = (
        "app.infrastructure",
        "httpx",
        "sqlalchemy",
        "dishka",
        "fastapi",
    )

    for source_file in application_files:
        source = source_file.read_text(encoding="utf-8")
        assert not any(name in source for name in forbidden), source_file


def test_auth_service_does_not_own_profile_provisioning() -> None:
    source = (SOURCE_ROOT / "application" / "services" / "auth_service.py").read_text(
        encoding="utf-8"
    )

    assert "ProfileProvisionerProtocol" not in source
    assert "def register" not in source
