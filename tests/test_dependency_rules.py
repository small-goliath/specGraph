"""의존 방향 회귀 가드 (CLAUDE.md `## 코드 컨벤션` — 의존 방향 · LightRAG 흡수 지점).

- 공유 모듈(``src/specgraph/*.py``)과 ``mcp_server`` 는 ``indexer`` 를 import 하지 않는다.
- LightRAG 패키지(``lightrag``)는 ``lightrag_store.py`` 에서만 import 한다.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parents[1] / "src" / "specgraph"


def _package_of(path: Path, root: Path) -> list[str]:
    """``path`` 가 속한 패키지의 절대 모듈 경로(상대 import 환산 기준)."""
    return [root.name, *path.relative_to(root).parent.parts]


def _imports(path: Path, root: Path = PACKAGE) -> set[str]:
    """절대 모듈 이름 집합. 상대 import 는 파일 위치로 환산하고, ``from pkg import mod`` 는
    ``pkg.mod`` 까지 펼친다(서브모듈 import 형태를 놓치지 않는다)."""
    package = _package_of(path, root)
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            parts = [*base, *([node.module] if node.module else [])]
            module = ".".join(parts)
            if module:
                modules.add(module)
            modules.update(
                f"{module}.{alias.name}" if module else alias.name for alias in node.names
            )
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


# --- 가드 자체의 음성 테스트: 우회 형태 (R3-A1 · R3-T4) ---------------------------------------


def _write(tmp_path: Path, relative: str, source: str) -> tuple[Path, Path]:
    root = tmp_path / "specgraph"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path, root


def _imports_indexer(path: Path, root: Path) -> bool:
    return any(m.startswith("specgraph.indexer") for m in _imports(path, root=root))


def test_imports_detects_from_specgraph_import_indexer(tmp_path):
    path, root = _write(tmp_path, "lookup.py", "from specgraph import indexer\n")

    assert _imports_indexer(path, root)


def test_imports_detects_import_specgraph_indexer_as_alias(tmp_path):
    path, root = _write(tmp_path, "lookup.py", "import specgraph.indexer.service as svc\n")

    assert _imports_indexer(path, root)


def test_imports_detects_relative_import_of_indexer_from_shared_module(tmp_path):
    path, root = _write(tmp_path, "manifest.py", "from .indexer import service\n")

    assert _imports_indexer(path, root)


def test_imports_detects_relative_import_of_indexer_from_mcp_server(tmp_path):
    path, root = _write(tmp_path, "mcp_server/lookup.py", "from ..indexer.service import X\n")

    assert _imports_indexer(path, root)


def test_imports_detects_relative_package_import_of_indexer_from_mcp_server(tmp_path):
    path, root = _write(tmp_path, "mcp_server/lookup.py", "from .. import indexer\n")

    assert _imports_indexer(path, root)


def test_imports_ignores_unrelated_relative_import(tmp_path):
    path, root = _write(
        tmp_path, "mcp_server/lookup.py", "from . import server\nfrom ..docid import X\n"
    )

    assert not _imports_indexer(path, root)
