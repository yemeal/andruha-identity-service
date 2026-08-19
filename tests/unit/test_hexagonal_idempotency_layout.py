from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "app"


def test_idempotency_is_organized_by_hexagonal_responsibility() -> None:
    expected_modules = {
        "application/exceptions/idempotency.py",
        "application/ports/dto/idempotency.py",
        "application/ports/idempotency/__init__.py",
        "application/services/durable_idempotency.py",
        "application/services/idempotency_coordinator.py",
        "application/services/idempotency_fingerprint.py",
        "application/value_objects/idempotency.py",
        "infrastructure/cache/valkey_idempotency_store.py",
        "infrastructure/resilience/circuit_breaking_hot_store.py",
    }

    assert all((SOURCE_ROOT / module).is_file() for module in expected_modules)
    assert not (SOURCE_ROOT / "application" / "idempotency").exists()
    assert not (SOURCE_ROOT / "infrastructure" / "idempotency").exists()


def test_inner_hexagonal_layers_do_not_import_outward_adapters() -> None:
    forbidden_imports = (
        "from app.infrastructure",
        "import app.infrastructure",
        "from app.entrypoints",
        "import app.entrypoints",
    )

    for layer in ("domain", "application"):
        for source_file in (SOURCE_ROOT / layer).rglob("*.py"):
            source = source_file.read_text(encoding="utf-8")
            assert not any(item in source for item in forbidden_imports), source_file
