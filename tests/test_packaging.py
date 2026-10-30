"""Every third-party module imported by src/ is a declared dependency (M6)."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Imported directly but guaranteed by a declared dependency: name -> provider.
TRANSITIVE = {"starlette": "fastapi", "pydantic": "fastapi"}


def _declared() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {re.split(r"[\[<>=!~ ]", d, maxsplit=1)[0].lower() for d in deps}


def _imported() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in sys.stdlib_module_names and m != "lanscape_atlas"}


def test_third_party_imports_are_declared() -> None:
    declared = _declared()
    missing = {
        m for m in _imported() if m.lower() not in declared and TRANSITIVE.get(m) not in declared
    }
    assert not missing, f"undeclared dependencies: {sorted(missing)}"
