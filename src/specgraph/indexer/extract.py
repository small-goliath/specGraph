"""결정적 추출 — 화면 ID · 정책 ID · § 참조 · 문서 간 참조 (AC13, AC19).

정규식은 계획 §2 초안을 단위 테스트로 고정한 것이다. 실문서 대조(M7) 후 보정은 테스트를 먼저
추가해서 한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from specgraph.docid import build_doc_id
from specgraph.kg_model import DEFINED_IN_HEADING, DEFINED_IN_TABLE, MENTIONED, CrossDocRef

SCREEN_ID = re.compile(r"(?<![A-Za-z0-9-])[A-Z]{2,5}(?:-[A-Z])?-\d{1,3}(?!\d|\.\d)")
POLICY_ID = re.compile(r"(?<![A-Za-z0-9-])[A-Z]-\d+(?:\.\d+)*(?!\d)")
SECTION_REF = re.compile(r"§\s?(\d+)(?:\.\d+)*")
_KIND = r"(?i:PRD|UI/UX\s?spec|tech-spec)"
# 문서 종류 키워드와 § 사이에 올 수 있는 조사("Admin PRD의 §5", "Admin PRD 에서 §3").
_PARTICLE = r"(?:\s*(?:의|에서|에))?"
# 이름 = 영문 단어 1~4개 또는 한글 단어 하나("Admin", "Partner Portal", "어드민").
CROSS_DOC_REF = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9/-]*(?: [A-Za-z][A-Za-z0-9/-]*){0,3}|[가-힣]+)\s+"
    rf"(?P<kind>{_KIND}){_PARTICLE}\s*§\s?(?P<section>\d+)(?:\.\d+)*"
)
# 이름이 없어도 문서 종류 키워드 바로 뒤의 § 는 이 문서의 § 참조가 아니다("상위 PRD §5").
_KIND_SECTION = re.compile(rf"{_KIND}{_PARTICLE}\s*§\s?\d+(?:\.\d+)*")

_KIND_NORMAL = {"prd": "prd", "ui/ux spec": "ui-ux-spec", "ui/uxspec": "ui-ux-spec"}
_KIND_TOKENS = {
    "prd": (("prd",),),
    "ui-ux-spec": (("uiux",), ("ui", "ux")),
    "tech-spec": (("tech", "spec"), ("techspec",)),
}


@dataclass(frozen=True)
class DocumentRef:
    branch: str
    path: str


@dataclass
class ChapterRefs:
    screens: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)
    section_refs: list[str] = field(default_factory=list)
    cross_doc_refs: list[CrossDocRef] = field(default_factory=list)
    screen_ranks: dict[str, int] = field(default_factory=dict)
    policy_ranks: dict[str, int] = field(default_factory=dict)


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def extract_screen_ids(text: str) -> list[str]:
    return _unique(m.group(0) for m in SCREEN_ID.finditer(text))


def extract_policy_ids(text: str) -> list[str]:
    return _unique(m.group(0) for m in POLICY_ID.finditer(text))


def _cross_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in CROSS_DOC_REF.finditer(text)]
    spans.extend(m.span() for m in _KIND_SECTION.finditer(text))
    return spans


def extract_section_refs(text: str) -> list[str]:
    spans = _cross_spans(text)
    found = (
        m.group(1)
        for m in SECTION_REF.finditer(text)
        if not any(start <= m.start() < end for start, end in spans)
    )
    return _unique(found)


def _normalize_kind(raw: str) -> str:
    key = raw.lower()
    return _KIND_NORMAL.get(key, key)


def extract_cross_doc_refs(text: str) -> list[CrossDocRef]:
    refs = [
        CrossDocRef(
            name=m.group("name").strip(),
            kind=_normalize_kind(m.group("kind")),
            section=m.group("section"),
        )
        for m in CROSS_DOC_REF.finditer(text)
    ]
    return list(dict.fromkeys(refs))


def _slug_tokens(value: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", value.lower()) if t]


def _contains(tokens: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(tuple(tokens[i : i + n]) == tuple(needle) for i in range(len(tokens)))


def _matches(slug: str, name_tokens: Sequence[str], kind: str) -> bool:
    tokens = _slug_tokens(slug)
    kind_ok = any(_contains(tokens, k) for k in _KIND_TOKENS.get(kind, ((kind,),)))
    return kind_ok and _contains(tokens, name_tokens)


def _narrow(
    candidates: list[DocumentRef], preferences: Sequence[Callable[[DocumentRef], bool]]
) -> list[DocumentRef]:
    """선호 조건을 차례로 적용해 후보를 줄인다(조건을 만족하는 후보가 없으면 그 조건은 건너뛴다)."""
    for prefer in preferences:
        if len(candidates) <= 1:
            break
        preferred = [d for d in candidates if prefer(d)]
        if preferred:
            candidates = preferred
    return candidates


def resolve_cross_doc_ref(
    ref: CrossDocRef, documents: Sequence[DocumentRef], from_branch: str | None = None
) -> str | None:
    """문서 간 참조를 doc_id 로 해석한다. 0개 · 끝내 2개 이상 매칭이면 None(unresolved).

    이름은 뒤에서부터 단어를 늘려 가며(가장 짧은 접미 → 긴 접미) 대조해, 앞 문장 조각이 이름에
    섞여 들어와도 해석되게 한다. 파일 stem 매칭을 브랜치 슬러그 매칭보다 우선한다.
    stem 후보가 여럿이면(같은 파일이 여러 브랜치에 있음) 브랜치 슬러그도 맞는 후보, 그다음
    참조하는 챕터와 같은 브랜치(``from_branch``)의 후보를 고른다. 한글 이름은 슬러그와 대조할 수
    없어 unresolved 다.
    """
    words = _slug_tokens(ref.name)
    for size in range(1, len(words) + 1):
        name_tokens = words[-size:]
        by_stem = list(
            dict.fromkeys(
                d for d in documents if _matches(PurePosixPath(d.path).stem, name_tokens, ref.kind)
            )
        )
        by_slug = list(
            dict.fromkeys(
                d for d in documents if _matches(d.branch.rsplit("/", 1)[-1], name_tokens, ref.kind)
            )
        )
        candidates = _narrow(
            by_stem or by_slug,
            [set(by_slug).__contains__, lambda d: d.branch == from_branch],
        )
        if len(candidates) == 1:
            doc = candidates[0]
            return build_doc_id(doc.branch, doc.path, ref.section)
        if len(candidates) > 1:
            continue
        return None
    return None


def _id_pattern(identifier: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9-]){re.escape(identifier)}(?![\d.]\d|\d)")


def definition_rank(text: str, identifier: str) -> int | None:
    """헤딩에 있으면 0, 표 첫 열이면 1, 그냥 언급이면 2, 없으면 None."""
    pattern = _id_pattern(identifier)
    if not pattern.search(text):
        return None
    rank = MENTIONED
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and pattern.search(stripped):
            return DEFINED_IN_HEADING
        if stripped.startswith("|"):
            cells = stripped.strip("|").split("|")
            if cells and cells[0].strip() == identifier:
                rank = DEFINED_IN_TABLE
    return rank


def extract_chapter(text: str) -> ChapterRefs:
    screens = extract_screen_ids(text)
    policies = extract_policy_ids(text)
    return ChapterRefs(
        screens=screens,
        policies=policies,
        section_refs=extract_section_refs(text),
        cross_doc_refs=extract_cross_doc_refs(text),
        screen_ranks={s: r for s in screens if (r := definition_rank(text, s)) is not None},
        policy_ranks={p: r for p in policies if (r := definition_rank(text, p)) is not None},
    )
