"""The core must not depend on any specific data source (docs/challenges.md, C9)."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).parents[1] / "src" / "multi_agent_ai_systems"
SOURCE_SPECIFIC = {"edgar.py", "config.py", "__main__.py"}  # config wires sources up; it is not core


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_core_does_not_import_source_connectors():
    core = [p for p in PACKAGE.rglob("*.py") if p.name not in SOURCE_SPECIFIC]
    assert core  # make sure we are checking something
    offenders = [
        f"{p.relative_to(PACKAGE)} imports {m}"
        for p in core
        for m in imported_modules(p)
        if "edgar" in m
    ]
    assert offenders == []
