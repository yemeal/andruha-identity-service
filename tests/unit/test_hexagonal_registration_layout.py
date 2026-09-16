import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "app"


@pytest.mark.parametrize("layer", ["domain", "application"])
def test_inner_layers_do_not_import_adapters(layer: str) -> None:
    forbidden = (
        "app.infrastructure",
        "app.entrypoints",
        "app.core",
        "sqlalchemy",
        "httpx",
        "dishka",
        "fastapi",
    )
    if layer == "domain":
        forbidden += ("app.application",)
    for source in (SOURCE_ROOT / layer).rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(
                module == name or module.startswith(name + ".")
                for module in modules
                for name in forbidden
            ), source
