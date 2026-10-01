"""단위 테스트용 가짜 포트 구현 — LightRAG · manifest.

FakeLightRAG 는 호출을 ``calls`` 에 기록하고, 챕터 삽입 · 삭제 때 실제 LightRAG 처럼 LLM 을
부른 것으로 간주해 ``LlmCallCounter.record()`` 를 호출한다(귀속 검증용).

``LightRagPort`` 계약과, 실제 LightRAG(lightrag-hku 1.5) 에서 확인한 다음 동작을 모델링한다
(근거 테스트: tests/test_lightrag_store.py 의 real_lightrag 테스트).

- **같은 doc_id 재삽입은 무시**: 상태와 무관하게 이미 있는 id 의 ``ainsert`` 는 아무것도 하지
  않는다. FAILED 로 남은 문서는 지우기 전까지 계속 FAILED 다
  (``test_real_lightrag_failed_insert_stays_failed_until_deleted``).
- **없는 문서 삭제는 not_found**: LLM 을 부르지 않는다.
- **엣지는 무방향**: 관계 (a, b) 와 (b, a) 는 같은 엣지다. ``upsert_chapter_kg`` 는 ``previous``
  의 관계를 지우되 ``keep``(다른 챕터 소유) 쌍은 남긴다. 챕터 KG 삭제는 그 챕터 엔티티에 닿은
  엣지를 모두 지운다(``adelete_by_entity``).

본문 해시 dedup(다른 doc_id · 같은 본문)은 어댑터가 머리 줄로 막는 LightRAG 내부 동작이라 포트
계약 밖이다. 가짜는 이를 모델링하지 않는다 — 회귀는 어댑터 테스트가 막는다
(``test_insert_body_header_is_unique_per_doc_and_never_spells_the_doc_id``,
``test_real_lightrag_identical_bodies_with_different_doc_ids_both_processed``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from specgraph.errors import IndexingError
from specgraph.kg_model import KgKeys, KgPayload
from specgraph.lightrag_store import RelationPair, Retrieval, undirected
from specgraph.llm import LlmCallCounter
from specgraph.manifest import ChapterRecord

PROCESSED = "processed"  # LightRAG DocStatus.PROCESSED 값
FAILED = "failed"


@dataclass
class FakeLightRAG:
    counter: LlmCallCounter | None = None
    llm_calls_per_insert: int = 2
    llm_calls_per_delete: int = 1
    llm_calls_per_merge: int = 0
    llm_calls_per_entity_delete: int = 0
    fail_on: set[str] = field(default_factory=set)
    fail_once: set[str] = field(default_factory=set)
    fail_kg_once: set[str] = field(default_factory=set)
    fail_merge_once: bool = False
    fail_entity_delete_once: bool = False
    extracted_entities: set[str] = field(default_factory=set)
    retrieval: Retrieval | None = None

    calls: list[tuple] = field(default_factory=list)
    chapters: dict[str, list[str]] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    kg: dict[str, KgPayload] = field(default_factory=dict)
    kg_entities: set[str] = field(default_factory=set)
    edges: set[RelationPair] = field(default_factory=set)
    merges: list[tuple[list[str], str]] = field(default_factory=list)

    def _llm(self, n: int) -> None:
        if self.counter is not None:
            for _ in range(n):
                self.counter.record()

    def _should_fail(self, doc_id: str) -> bool:
        for marker in list(self.fail_once):
            if marker in doc_id:
                self.fail_once.discard(marker)
                return True
        return any(marker in doc_id for marker in self.fail_on)

    async def insert_chapter(self, doc_id: str, blocks: list[str]) -> None:
        self.calls.append(("insert", doc_id))
        if doc_id in self.statuses:  # 같은 id 는 상태와 무관하게 무시된다
            if self.statuses[doc_id] != PROCESSED:
                raise IndexingError(f"fake: doc_id={doc_id} status={self.statuses[doc_id]}")
            return
        self._llm(self.llm_calls_per_insert)
        if self._should_fail(doc_id):
            self.statuses[doc_id] = FAILED
            raise IndexingError(f"fake: forced failure doc_id={doc_id} status={FAILED}")
        self.statuses[doc_id] = PROCESSED
        self.chapters[doc_id] = list(blocks)

    async def delete_chapter(self, doc_id: str) -> None:
        self.calls.append(("delete", doc_id))
        if doc_id not in self.statuses:  # not_found
            return
        self._llm(self.llm_calls_per_delete)
        self.statuses.pop(doc_id, None)
        self.chapters.pop(doc_id, None)

    async def upsert_chapter_kg(
        self,
        payload: KgPayload,
        previous: KgKeys | None,
        keep: frozenset[RelationPair] = frozenset(),
    ) -> None:
        self.calls.append(("upsert_kg", payload.keys.chapter_entity))
        if self._should_fail_kg(payload.keys.chapter_entity):
            raise IndexingError(f"fake: custom KG upsert failed {payload.keys.chapter_entity}")
        kept = {undirected(p) for p in keep}
        if previous is not None:
            for pair in previous.relations:
                if undirected(pair) not in kept:
                    self.edges.discard(undirected(pair))
        self.edges.update(undirected(pair) for pair in payload.keys.relations)
        self.kg[payload.keys.chapter_entity] = payload
        for entity in payload.custom_kg["entities"]:
            self.kg_entities.add(entity["entity_name"])

    async def delete_chapter_kg(self, keys: KgKeys) -> None:
        self.calls.append(("delete_kg", keys.chapter_entity))
        self.edges = {e for e in self.edges if keys.chapter_entity not in e}
        self.kg.pop(keys.chapter_entity, None)
        self.kg_entities.discard(keys.chapter_entity)

    def _should_fail_kg(self, chapter: str) -> bool:
        if chapter in self.fail_kg_once:
            self.fail_kg_once.discard(chapter)
            return True
        return False

    def has_edge(self, a: str, b: str) -> bool:
        return undirected((a, b)) in self.edges

    async def delete_entities(self, names: list[str]) -> None:
        self.calls.append(("delete_entities", tuple(sorted(names))))
        if self.fail_entity_delete_once:
            self.fail_entity_delete_once = False
            raise IndexingError("fake: entity delete failed")
        for name in names:
            self._llm(self.llm_calls_per_entity_delete)
            self.kg_entities.discard(name)

    async def entity_names(self) -> list[str]:
        return sorted(self.extracted_entities | self.kg_entities)

    async def merge_entities(self, sources: list[str], target: str) -> None:
        self.calls.append(("merge", tuple(sources), target))
        if self.fail_merge_once:
            self.fail_merge_once = False
            raise IndexingError("fake: merge failed")
        self._llm(self.llm_calls_per_merge)
        self.merges.append((list(sources), target))
        for name in sources:
            self.extracted_entities.discard(name)
        self.extracted_entities.add(target)

    async def retrieve(self, question: str, mode: str, top_k: int | None = None) -> Retrieval:
        self.calls.append(("retrieve", question, mode))
        return self.retrieval or Retrieval()

    def doc_ids(self, prefix: str = "") -> list[str]:
        return sorted(d for d in self.chapters if d.startswith(prefix))


@dataclass
class InMemoryManifest:
    heads: dict[str, str] = field(default_factory=dict)
    records: dict[str, ChapterRecord] = field(default_factory=dict)
    fail_upsert_once: set[str] = field(default_factory=set)
    # 내용 해시가 있는(= 삽입 완료를 기록하는) upsert 만 한 번 실패시킨다.
    fail_commit_upsert_once: set[str] = field(default_factory=set)

    async def branch_heads(self) -> dict[str, str]:
        return dict(self.heads)

    async def chapter_branches(self) -> set[str]:
        return {r.branch for r in self.records.values()}

    async def set_branch_head(self, branch: str, sha: str) -> None:
        self.heads[branch] = sha

    async def delete_branch_head(self, branch: str) -> None:
        self.heads.pop(branch, None)

    async def chapters_for_branch(self, branch: str) -> dict[str, ChapterRecord]:
        return {d: r for d, r in self.records.items() if r.branch == branch}

    async def upsert_chapter(self, record: ChapterRecord) -> None:
        if record.doc_id in self.fail_upsert_once:
            self.fail_upsert_once.discard(record.doc_id)
            raise RuntimeError(f"fake: manifest upsert failed doc_id={record.doc_id}")
        if record.content_hash and record.doc_id in self.fail_commit_upsert_once:
            self.fail_commit_upsert_once.discard(record.doc_id)
            raise RuntimeError(f"fake: manifest commit upsert failed doc_id={record.doc_id}")
        self.records[record.doc_id] = record

    async def touch_chapters(self, doc_ids: list[str], commit_sha: str) -> None:
        for doc_id in doc_ids:
            self.records[doc_id] = self.records[doc_id].with_commit(commit_sha)

    async def delete_chapter(self, doc_id: str) -> None:
        self.records.pop(doc_id, None)

    async def get_chapter(self, doc_id: str) -> ChapterRecord | None:
        return self.records.get(doc_id)

    async def all_chapters(self) -> list[ChapterRecord]:
        return [self.records[d] for d in sorted(self.records)]

    async def chapters_with_screen(self, screen_id: str) -> list[ChapterRecord]:
        return [r for r in await self.all_chapters() if screen_id in r.screens]

    async def chapters_with_policy(self, policy_id: str) -> list[ChapterRecord]:
        return [r for r in await self.all_chapters() if policy_id in r.policies]

    async def referenced_names(self, names: list[str]) -> set[str]:
        used: set[str] = set()
        for r in self.records.values():
            used.update(r.screens)
            used.update(r.policies)
            used.add(r.document)
        return used & set(names)
