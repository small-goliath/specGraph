"""챕터 분할 · 내용 해시 (AC8, AC9 — 결정 D5).

챕터
    문서에서 처음 나타난 **번호 헤딩**(``## 5. 정산``, ``# 5 …``, ``## §5 …``)의 레벨을 기준으로,
    같은 레벨의 번호 헤딩마다 챕터를 나눈다. ``5.1`` 같은 하위 번호는 경계가 아니다.
    첫 번호 헤딩 앞의 머리말은 ``preamble`` 섹션이다. 코드펜스 안의 헤딩은 무시한다.
    같은 번호가 다시 나오면(문서 오류) 새 챕터를 만들지 않고 앞 챕터에 이어 붙인다
    (doc_id 충돌 방지, 내용 손실 없음).

블록 분할(표 보존, D7 · AC10)은 공유 모듈 ``specgraph.blocks`` 에 있다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from specgraph.blocks import fence_marker
from specgraph.docid import PREAMBLE

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_NUMBER = re.compile(r"^(?:§\s*)?(\d+)(?:\.(?!\d))?(?:\s|$)")


@dataclass(frozen=True)
class Chapter:
    section: str
    title: str
    text: str

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


def content_hash(text: str) -> str:
    """줄 끝 공백과 빈 줄 개수만 정규화한 sha256. 그 외 변경은 모두 해시를 바꾼다."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    normalized: list[str] = []
    for line in lines:
        if line == "" and normalized and normalized[-1] == "":
            continue
        normalized.append(line)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _numbered_heading(line: str) -> tuple[int, str, str] | None:
    """(레벨, 번호, 제목) — 번호 헤딩이 아니면 None."""
    m = _HEADING.match(line)
    if not m:
        return None
    title = m.group(2)
    num = _NUMBER.match(title)
    if not num:
        return None
    return len(m.group(1)), num.group(1), title


def split_chapters(markdown: str) -> list[Chapter]:
    lines = markdown.splitlines()
    base_level: int | None = None
    chapters: list[tuple[str, str, list[str]]] = [(PREAMBLE, "", [])]
    seen: set[str] = set()
    fence: str | None = None

    for line in lines:
        marker = fence_marker(line)
        if fence is not None:
            if marker == fence:
                fence = None
            chapters[-1][2].append(line)
            continue
        if marker is not None:
            fence = marker
            chapters[-1][2].append(line)
            continue

        heading = _numbered_heading(line)
        if heading is not None:
            level, number, title = heading
            if base_level is None:
                base_level = level
            if level == base_level and number not in seen:
                seen.add(number)
                chapters.append((number, title, [line]))
                continue
        chapters[-1][2].append(line)

    result: list[Chapter] = []
    for section, title, body in chapters:
        text = "\n".join(body).strip("\n")
        if section == PREAMBLE and not text.strip():
            continue
        result.append(Chapter(section=section, title=title, text=text))
    return result
