"""결정적 그래프 층 — 챕터 하나의 LightRAG custom KG payload (AC13).

LLM 을 쓰지 않는 순수 함수다. 엔티티는 CHAPTER(doc_id) · DOCUMENT(``<branch>:<path>``) ·
SCREEN · POLICY, 관계는 PART_OF · DEFINES · MENTIONS · REFERS_TO 이고 모두 챕터에서 나간다.
모든 엔티티 · 관계의 ``source_id`` 는 이 챕터의 앵커 청크(별칭 = doc_id)를 가리키며,
``file_path`` 는 doc_id 다(어댑터가 LightRAG 저장 형식으로 인코딩한다).

``KgKeys``(공유 모듈 ``specgraph.kg_model``)는 이 챕터가 소유한 그래프 요소다. 재삽입 시에는
소유 관계만 지우고 챕터 엔티티는 upsert 해서, 다른 챕터에서 들어오는 REFERS_TO 엣지를 보존한다.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from specgraph.docid import build_doc_id, parse_doc_id
from specgraph.indexer.extract import ChapterRefs, DocumentRef, resolve_cross_doc_ref
from specgraph.kg_model import DEFINED_IN_TABLE, CrossDocRef, KgKeys, KgPayload

CHAPTER = "CHAPTER"
DOCUMENT = "DOCUMENT"
SCREEN = "SCREEN"
POLICY = "POLICY"

PART_OF = "PART_OF"
DEFINES = "DEFINES"
MENTIONS = "MENTIONS"
REFERS_TO = "REFERS_TO"


def _anchor_content(doc_id: str, title: str, refs: ChapterRefs) -> str:
    lines = [f"[{doc_id}] {title}".rstrip()]
    if refs.screens:
        lines.append("화면: " + ", ".join(refs.screens))
    if refs.policies:
        lines.append("정책: " + ", ".join(refs.policies))
    return "\n".join(lines)


def build_chapter_kg(
    doc_id: str,
    title: str,
    refs: ChapterRefs,
    documents: Sequence[DocumentRef],
    chapters: Collection[str] | None = None,
) -> KgPayload:
    """``chapters`` 를 주면 그 안에 없는 챕터로의 REFERS_TO 는 만들지 않고 ``dangling`` 으로 알린다
    (LightRAG 는 없는 끝점을 UNKNOWN placeholder 노드로 만들기 때문이다). 대상이 나중에 생기면
    서비스의 참조 재해석이 관계를 추가한다."""
    parsed = parse_doc_id(doc_id)
    dangling: list[str] = []

    def exists(target: str) -> bool:
        if chapters is None or target in chapters:
            return True
        if target not in dangling:
            dangling.append(target)
        return False

    document = parsed.document
    anchor = _anchor_content(doc_id, title, refs)

    def entity(name: str, entity_type: str, description: str) -> dict[str, Any]:
        return {
            "entity_name": name,
            "entity_type": entity_type,
            "description": description,
            "source_id": doc_id,
            "file_path": doc_id,
        }

    entities = [
        entity(doc_id, CHAPTER, f"챕터 §{parsed.section} {title} ({doc_id})"),
        entity(document, DOCUMENT, f"문서 {parsed.path} (브랜치 {parsed.branch})"),
    ]
    relations: dict[tuple[str, str], dict[str, Any]] = {}

    def relate(target: str, keyword: str, description: str) -> None:
        if target == doc_id or (doc_id, target) in relations:
            return
        relations[(doc_id, target)] = {
            "src_id": doc_id,
            "tgt_id": target,
            "description": description,
            "keywords": keyword,
            "weight": 1.0,
            "source_id": doc_id,
            "file_path": doc_id,
        }

    relate(document, PART_OF, f"{doc_id} 는 {document} 의 챕터다")
    for screen in refs.screens:
        entities.append(entity(screen, SCREEN, f"화면 ID {screen}"))
        rank = refs.screen_ranks.get(screen)
        keyword = DEFINES if rank is not None and rank <= DEFINED_IN_TABLE else MENTIONS
        relate(screen, keyword, f"{doc_id} 가 화면 {screen} 을(를) {keyword}")
    for policy in refs.policies:
        entities.append(entity(policy, POLICY, f"정책 ID {policy}"))
        rank = refs.policy_ranks.get(policy)
        keyword = DEFINES if rank is not None and rank <= DEFINED_IN_TABLE else MENTIONS
        relate(policy, keyword, f"{doc_id} 가 정책 {policy} 을(를) {keyword}")
    for section in refs.section_refs:
        target = build_doc_id(parsed.branch, parsed.path, section)
        if target != doc_id and exists(target):
            relate(target, REFERS_TO, f"{doc_id} 가 같은 문서 §{section} 을 참조")

    unresolved: list[CrossDocRef] = []
    for ref in refs.cross_doc_refs:
        target = resolve_cross_doc_ref(ref, documents, from_branch=parsed.branch)
        if target is None:
            unresolved.append(ref)
            continue
        if exists(target):
            relate(target, REFERS_TO, f"{doc_id} 가 {ref.name} {ref.kind} §{ref.section} 을 참조")

    custom_kg = {
        "chunks": [
            {
                "content": anchor,
                "source_id": doc_id,
                "file_path": doc_id,
                "chunk_order_index": 0,
            }
        ],
        "entities": entities,
        "relationships": list(relations.values()),
    }
    keys = KgKeys(chapter_entity=doc_id, relations=tuple(relations), anchor_content=anchor)
    return KgPayload(custom_kg=custom_kg, keys=keys, unresolved=unresolved, dangling=dangling)
