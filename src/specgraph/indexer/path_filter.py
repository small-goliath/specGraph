"""인덱싱 대상 경로 필터 (AC11).

포함: include_dirs(기본 prd · ui-ux-spec · tech-spec · qa) 아래의 ``.md``.
제외: README.md · CLAUDE.md(어느 위치든), ``.md`` 가 아닌 모든 파일(.docx · .png 등).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath

EXCLUDED_NAMES = frozenset({"readme.md", "claude.md"})


def is_indexable(path: str, include_dirs: Sequence[str]) -> bool:
    p = PurePosixPath(path)
    if p.suffix.lower() != ".md":
        return False
    if p.name.lower() in EXCLUDED_NAMES:
        return False
    return len(p.parts) >= 2 and p.parts[0] in include_dirs


def filter_paths(paths: Iterable[str], include_dirs: Sequence[str]) -> list[str]:
    return [p for p in paths if is_indexable(p, include_dirs)]
