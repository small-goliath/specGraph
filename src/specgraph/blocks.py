"""챕터 블록 분할 (AC10 — 결정 D7). 공유 순수 모듈: indexer 와 LightRAG 어댑터가 함께 쓴다.

LightRAG 의 토큰 청킹이 표를 자르지 않도록 챕터를 문단 · 표 · 코드펜스 단위로 나눈 뒤
``max_chars`` 이하로 묶는다. 표 하나가 한도를 넘으면 **행 경계**에서 나누고 헤더 행을
반복한다. 블록은 ``BLOCK_SEPARATOR`` 로 이어 ``split_by_character`` 로 넘긴다.
크기는 ``measure``(기본 문자 수)로 잰다. LightRAG 어댑터는 같은 함수를 토크나이저 토큰 수로
다시 적용해(``fit_blocks``) 모든 블록이 청크 토큰 한도 안에 들게 한다.

표는 선행 파이프가 있는 행(``| a | b |``)과, 선행 파이프가 없는 GFM 표(``a | b`` 다음 줄이
``--- | ---`` 구분행)를 모두 인식한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

BLOCK_SEPARATOR = "\n\n<<<SPECGRAPH-BLOCK>>>\n\n"
TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")

Measure = Callable[[str], int]


def fence_marker(line: str) -> str | None:
    """코드펜스 시작/끝 표시(```` ``` ```` · ``~~~``)면 그 표시, 아니면 None."""
    stripped = line.lstrip()
    for marker in ("```", "~~~"):
        if stripped.startswith(marker):
            return marker
    return None


@dataclass(frozen=True)
class _Unit:
    kind: str  # "text" | "table" | "fence"
    text: str


def _is_piped_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _starts_pipeless_table(lines: list[str], i: int) -> bool:
    """``a | b`` 다음 줄이 ``--- | ---`` 구분행이면 선행 파이프 없는 GFM 표의 시작이다."""
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and "|" in lines[i + 1]
        and bool(TABLE_SEPARATOR.match(lines[i + 1].strip()))
    )


def _units(text: str) -> list[_Unit]:
    lines = text.splitlines()
    units: list[_Unit] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        marker = fence_marker(line)
        if marker is not None:
            j = i + 1
            while j < len(lines) and fence_marker(lines[j]) != marker:
                j += 1
            j = min(j + 1, len(lines))
            units.append(_Unit("fence", "\n".join(lines[i:j])))
            i = j
            continue
        if _is_piped_row(line):
            j = i
            while j < len(lines) and _is_piped_row(lines[j]):
                j += 1
            units.append(_Unit("table", "\n".join(lines[i:j])))
            i = j
            continue
        if _starts_pipeless_table(lines, i):
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                j += 1
            units.append(_Unit("table", "\n".join(lines[i:j])))
            i = j
            continue
        j = i
        while (
            j < len(lines)
            and lines[j].strip()
            and not _is_piped_row(lines[j])
            and fence_marker(lines[j]) is None
            and not (j > i and _starts_pipeless_table(lines, j))
        ):
            j += 1
        units.append(_Unit("text", "\n".join(lines[i:j])))
        i = j
    return units


def _split_table(table: str, max_chars: int, measure: Measure) -> list[str]:
    rows = table.splitlines()
    header_len = 2 if len(rows) > 1 and TABLE_SEPARATOR.match(rows[1].strip()) else 1
    header, body = rows[:header_len], rows[header_len:]
    pieces: list[str] = []
    current: list[str] = []
    for row in body:
        candidate = "\n".join(header + current + [row])
        if current and measure(candidate) > max_chars:
            pieces.append("\n".join(header + current))
            current = [row]
        else:
            current.append(row)
    if current or not pieces:
        pieces.append("\n".join(header + current))
    return pieces


def split_blocks(text: str, max_chars: int, measure: Measure = len) -> list[str]:
    """``max_chars`` 는 ``measure`` 단위의 블록 한도다(기본은 문자 수)."""
    pieces: list[str] = []
    for unit in _units(text):
        if unit.kind == "table" and measure(unit.text) > max_chars:
            pieces.extend(_split_table(unit.text, max_chars, measure))
        else:
            pieces.append(unit.text)

    blocks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif measure(f"{current}\n\n{piece}") <= max_chars:
            current = f"{current}\n\n{piece}"
        else:
            blocks.append(current)
            current = piece
    if current:
        blocks.append(current)
    return blocks


def fit_blocks(blocks: list[str], max_size: int, measure: Measure) -> list[str]:
    """한도를 넘는 블록만 ``measure`` 기준으로 다시 나눈다(표는 행 경계, 헤더 반복).

    단일 행 · 문단 · 코드펜스가 혼자 한도를 넘으면 자르지 않고 그대로 둔다(호출자가 판단).
    """
    fitted: list[str] = []
    for block in blocks:
        if measure(block) <= max_size:
            fitted.append(block)
        else:
            fitted.extend(split_blocks(block, max_size, measure))
    return fitted


def join_blocks(blocks: list[str]) -> str:
    return BLOCK_SEPARATOR.join(blocks)
