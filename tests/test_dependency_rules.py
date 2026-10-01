"""의존 방향 회귀 가드 (CLAUDE.md `## 코드 컨벤션` — 의존 방향 · LightRAG 흡수 지점).

- 공유 모듈(``src/specgraph/*.py``)과 ``mcp_server`` 는 ``indexer`` 를 import 하지 않는다.
- LightRAG 패키지(``lightrag``)는 ``lightrag_store.py`` 에서만 import 한다.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parents[1] / "src" / "specgraph"


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _shared_and_mcp_modules() -> list[Path]:
    return sorted([*PACKAGE.glob("*.py"), *(PACKAGE / "mcp_server").rglob("*.py")])


@pytest.mark.parametrize("path", _shared_and_mcp_modules(), ids=lambda p: p.name)
def test_shared_and_mcp_modules_do_not_import_indexer(path):
    offenders = sorted(m for m in _imports(path) if m.startswith("specgraph.indexer"))

    assert offenders == []


def test_only_lightrag_store_imports_lightrag():
    offenders = sorted(
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if path.name != "lightrag_store.py"
        and any(m == "lightrag" or m.startswith("lightrag.") for m in _imports(path))
    )

    assert offenders == []
