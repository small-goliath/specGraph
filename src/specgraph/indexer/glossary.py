""" "0. 용어" 표 → 엔티티 정규화 사전 (AC14).

- 1열이 표준어. 셀 안 괄호 표기(``파트너 (Partner)``)는 별칭이다.
- 헤더가 동의어 · 별칭 · 영문 계열인 열의 값(``,`` ``/`` ``·`` 구분)도 별칭이다.
- 대조는 대소문자 무시 · 연속 공백 하나로 정규화해서 한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from specgraph.indexer.chapters import Chapter

ALIAS_HEADERS = ("동의어", "별칭", "영문", "약어", "synonym", "alias", "english")
_PAREN = re.compile(r"^(?P<base>[^()（）]+?)\s*[(（](?P<alias>[^()（）]+)[)）]\s*$")
_ALIAS_SPLIT = re.compile(r"\s*[,/·;]\s*")
_SEPARATOR_ROW = re.compile(r"^:?-{3,}:?$")


def normalize_term(term: str) -> str:
    return " ".join(term.split()).casefold()


@dataclass(frozen=True)
class Glossary:
    terms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.terms)

    def _index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for canonical, aliases in self.terms.items():
            index.setdefault(normalize_term(canonical), canonical)
            for alias in aliases:
                index.setdefault(normalize_term(alias), canonical)
        return index

    def canonical_for(self, name: str) -> str | None:
        return self._index().get(normalize_term(name))

    def merge(self, other: Glossary) -> Glossary:
        combined: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in self.terms.items()}
        for canonical, aliases in other.terms.items():
            existing = combined.get(canonical, ())
            combined[canonical] = tuple(dict.fromkeys((*existing, *aliases)))
        return Glossary(combined)

    def merge_plan(self, entity_names: Iterable[str]) -> list[tuple[str, list[str]]]:
        """기존 엔티티 이름 중 별칭인 것을 표준어로 합칠 계획. [(표준어, [별칭 엔티티…])]."""
        index = self._index()
        plan: dict[str, list[str]] = {}
        for name in entity_names:
            canonical = index.get(normalize_term(name))
            if canonical is None or name == canonical:
                continue
            plan.setdefault(canonical, []).append(name)
        order = list(self.terms)
        return sorted(plan.items(), key=lambda item: order.index(item[0]))


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _first_table(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            rows.append(_cells(line))
        elif rows:
            break
    return rows


def _is_glossary_chapter(chapter: Chapter) -> bool:
    return chapter.section == "0" and "용어" in chapter.title


def _split_aliases(cell: str) -> list[str]:
    return [a for a in _ALIAS_SPLIT.split(cell.strip()) if a]


def parse_table(rows: Sequence[Sequence[str]]) -> Glossary:
    if len(rows) < 2:
        return Glossary()
    header = [h.casefold() for h in rows[0]]
    alias_cols = [
        i for i, h in enumerate(header) if i > 0 and any(key in h for key in ALIAS_HEADERS)
    ]
    terms: dict[str, tuple[str, ...]] = {}
    for row in rows[1:]:
        if not row or all(_SEPARATOR_ROW.match(c) or not c for c in row):
            continue
        first = row[0]
        if not first:
            continue
        aliases: list[str] = []
        m = _PAREN.match(first)
        canonical = first
        if m:
            canonical = m.group("base").strip()
            aliases.extend(_split_aliases(m.group("alias")))
        for col in alias_cols:
            if col < len(row):
                aliases.extend(_split_aliases(row[col]))
        aliases = [a for a in dict.fromkeys(aliases) if a != canonical]
        existing = terms.get(canonical, ())
        terms[canonical] = tuple(dict.fromkeys((*existing, *aliases)))
    return Glossary(terms)


def parse_glossary(chapters: Iterable[Chapter]) -> Glossary:
    for chapter in chapters:
        if _is_glossary_chapter(chapter):
            return parse_table(_first_table(chapter.text))
    return Glossary()
