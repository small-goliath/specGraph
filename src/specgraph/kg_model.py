"""결정적 그래프 층의 공유 타입 (AC13 · AC19). 순수 모듈: indexer · LightRAG 어댑터 · manifest ·
mcp_server 가 함께 쓴다.

- ``KgKeys`` 챕터 하나가 소유한 그래프 요소(챕터 엔티티 · 관계 · 앵커 청크 내용).
- ``KgPayload`` LightRAG ``ainsert_custom_kg`` 에 넘길 payload 와 그 소유 키.
- ``CrossDocRef`` 문서 간 참조("Admin PRD §5").
- 정의 순위: 헤딩(0) < 표 첫 열(1) < 언급(2). ``choose_definition`` 이 정의 챕터를 고른다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

DEFINED_IN_HEADING = 0
DEFINED_IN_TABLE = 1
MENTIONED = 2


@dataclass(frozen=True)
class CrossDocRef:
    name: str
    kind: str  # "prd" | "ui-ux-spec" | "tech-spec"
    section: str


@dataclass(frozen=True)
class KgKeys:
    chapter_entity: str
    relations: tuple[tuple[str, str], ...]
    anchor_content: str
    # 삽입 시도 중 만들었지만 완료 기록에 이르지 못한 앵커 청크 내용 — PENDING 에서만 값이 있다.
    # 다음 시도가 ``anchor_content`` 와 함께 지워, 중간 시도의 청크가 누적되지 않게 한다.
    stale_anchors: tuple[str, ...] = ()

    @property
    def all_anchors(self) -> tuple[str, ...]:
        """이 레코드가 기억하는 모든 앵커 청크 내용(현재 + 중간 시도)."""
        return tuple(dict.fromkeys((self.anchor_content, *self.stale_anchors)))

    def to_json(self) -> str:
        return json.dumps(
            {
                "chapter_entity": self.chapter_entity,
                "relations": [list(r) for r in self.relations],
                "anchor_content": self.anchor_content,
                "stale_anchors": list(self.stale_anchors),
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> KgKeys:
        data = json.loads(raw)
        return cls(
            chapter_entity=data["chapter_entity"],
            relations=tuple((src, tgt) for src, tgt in data["relations"]),
            anchor_content=data["anchor_content"],
            stale_anchors=tuple(data.get("stale_anchors") or ()),
        )


@dataclass
class KgPayload:
    custom_kg: dict[str, Any]
    keys: KgKeys
    unresolved: list[CrossDocRef] = field(default_factory=list)
    # 참조 대상 챕터가 아직(또는 더 이상) 없어 관계를 만들지 않은 doc_id
    # (C14 — LightRAG placeholder 노드 방지).
    dangling: list[str] = field(default_factory=list)


def choose_definition(candidates: Sequence[tuple[str, int]]) -> str | None:
    """(doc_id, rank) 중 rank 가 가장 낮은 것, 동률이면 먼저 나온 것."""
    best: tuple[int, int, str] | None = None
    for order, (doc_id, rank) in enumerate(candidates):
        key = (rank, order, doc_id)
        if best is None or key < best:
            best = key
    return best[2] if best else None
