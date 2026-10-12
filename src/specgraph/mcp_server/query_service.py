"""질의 서비스 — 근거 반환 전용 (AC15 · AC16 · AC17, PPS-350 AC11~AC16).

1. 호출자(Claude Code)가 넘긴 ``hl_keywords`` · ``ll_keywords`` 로 LightRAG 구조화 검색
   (``aquery_data`` — LLM 생성 없이 근거만)을 해 청크 · 엔티티 · 관계를 얻는다. 키워드가 있으면
   LightRAG 는 LLM 키워드 추출을 건너뛰고, 둘 다 비면 어댑터가 질의문을 ``ll_keywords`` 로 쓴다.
   **질의 경로의 LLM 호출은 0 건이다** — 이 서비스는 LLM 을 갖지 않는다.
2. branch 가 주어지면 doc_id 접두어(``<branch>:``)로 모든 근거를 거른다.
3. 검색된 챕터의 결정적 참조(REFERS_TO — "Admin PRD §5", "§5.2")를 한 단계 따라가
   참조 대상 챕터를 ``expanded=True`` 청크로 추가한다(branch 필터가 있으면 그 브랜치 안에서만).
4. 숨긴 챕터(PENDING 첫 삽입)는 청크에서 빼고, 엔티티 · 관계는 보이는 출처만 ``sources`` 에
   붙이며 보이는 출처가 하나도 없으면 뺀다.
5. 모든 항목에 doc_id 와 manifest 의 commit_sha 를 붙인다. 근거가 없으면 빈 목록 +
   ``no_evidence=True`` 로 정상 반환한다(오류 아님). 답변 생성은 호출자의 몫이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from specgraph.docid import belongs_to_branch, parse_doc_id
from specgraph.errors import InvalidArgumentError
from specgraph.lightrag_store import QUERY_MODES, LightRagPort, Retrieval
from specgraph.manifest import ChapterRecord, ManifestPort

EXPANDED_CHAPTER_CHARS = 4000


@dataclass(frozen=True)
class Source:
    doc_id: str
    commit_sha: str

    def to_dict(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "commit_sha": self.commit_sha}


@dataclass(frozen=True)
class EvidenceChunk:
    doc_id: str
    commit_sha: str
    content: str
    expanded: bool = False  # 검색 결과가 아니라 참조 확장(REFERS_TO)으로 덧붙인 챕터 본문

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "commit_sha": self.commit_sha,
            "content": self.content,
            "expanded": self.expanded,
        }


@dataclass(frozen=True)
class EvidenceEntity:
    name: str
    entity_type: str
    description: str
    sources: tuple[Source, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass(frozen=True)
class EvidenceRelation:
    src: str
    tgt: str
    keywords: str
    description: str
    sources: tuple[Source, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "tgt": self.tgt,
            "keywords": self.keywords,
            "description": self.description,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class QueryResult:
    chunks: list[EvidenceChunk] = field(default_factory=list)
    entities: list[EvidenceEntity] = field(default_factory=list)
    relations: list[EvidenceRelation] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    mode: str = "mix"
    branch: str | None = None
    no_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "branch": self.branch,
            "no_evidence": self.no_evidence,
            "chunks": [c.to_dict() for c in self.chunks],
            "entities": [e.to_dict() for e in self.entities],
            "relations": [r.to_dict() for r in self.relations],
            "sources": [s.to_dict() for s in self.sources],
        }


def _is_chapter_id(value: str) -> bool:
    try:
        parse_doc_id(value)
    except ValueError:
        return False
    return True


def _clean_keywords(keywords: list[str] | None) -> list[str]:
    return [k.strip() for k in (keywords or ()) if k and k.strip()]


def _filter(retrieval: Retrieval, branch: str | None) -> Retrieval:
    """branch 가 있으면 그 브랜치 근거만 남긴다.

    엔티티 · 관계의 설명은 LightRAG 가 여러 출처를 합쳐 만든 글이므로, 출처에 다른 브랜치가 하나라도
    섞여 있으면 통째로 뺀다(설명의 일부만 걸러낼 수 없다 — AC16).
    """
    if branch is None:
        return retrieval

    def only_branch(doc_ids: tuple[str, ...]) -> bool:
        return bool(doc_ids) and all(belongs_to_branch(d, branch) for d in doc_ids)

    return Retrieval(
        chunks=[c for c in retrieval.chunks if belongs_to_branch(c.doc_id, branch)],
        entities=[e for e in retrieval.entities if only_branch(e.doc_ids)],
        relations=[r for r in retrieval.relations if only_branch(r.doc_ids)],
    )


class QueryService:
    def __init__(self, rag: LightRagPort, manifest: ManifestPort) -> None:
        self.rag = rag
        self.manifest = manifest

    async def query(
        self,
        question: str,
        mode: str = "mix",
        branch: str | None = None,
        hl_keywords: list[str] | None = None,
        ll_keywords: list[str] | None = None,
    ) -> QueryResult:
        if mode not in QUERY_MODES:
            raise InvalidArgumentError(
                f"mode 는 {', '.join(QUERY_MODES)} 중 하나여야 한다: {mode!r}"
            )
        if not question or not question.strip():
            raise InvalidArgumentError("question 이 비어 있다")
        branch = branch or None

        retrieval = _filter(
            await self.rag.retrieve(
                question,
                mode,
                hl_keywords=_clean_keywords(hl_keywords),
                ll_keywords=_clean_keywords(ll_keywords),
            ),
            branch,
        )

        retrieved_ids: list[str] = []
        for doc_id in (
            *(c.doc_id for c in retrieval.chunks),
            *(d for e in retrieval.entities for d in e.doc_ids),
            *(d for r in retrieval.relations for d in r.doc_ids),
        ):
            if doc_id not in retrieved_ids:
                retrieved_ids.append(doc_id)

        records: dict[str, ChapterRecord] = {}
        for doc_id in retrieved_ids:
            record = await self.manifest.get_chapter(doc_id)
            if record is not None and not record.is_hidden:
                records[doc_id] = record

        expanded: list[str] = []
        for doc_id in list(records):
            # PENDING 의 kg_keys 는 옛 · 새 시도의 관계를 합친 값이라 참조 확장에 쓰지 않는다.
            keys = None if records[doc_id].is_pending else records[doc_id].kg_keys
            for _, target in keys.relations if keys else ():
                if target in records or target in expanded or not _is_chapter_id(target):
                    continue
                if branch is not None and not belongs_to_branch(target, branch):
                    continue
                record = await self.manifest.get_chapter(target)
                if record is not None and not record.is_hidden:
                    expanded.append(target)
                    records[target] = record

        def visible_sources(doc_ids: tuple[str, ...]) -> tuple[Source, ...]:
            return tuple(Source(d, records[d].commit_sha) for d in doc_ids if d in records)

        chunks = [
            EvidenceChunk(c.doc_id, records[c.doc_id].commit_sha, c.content)
            for c in retrieval.chunks
            if c.doc_id in records
        ]
        chunks.extend(
            EvidenceChunk(
                d, records[d].commit_sha, records[d].content[:EXPANDED_CHAPTER_CHARS], expanded=True
            )
            for d in expanded
        )
        entities = [
            EvidenceEntity(e.name, e.entity_type, e.description, sources)
            for e in retrieval.entities
            if (sources := visible_sources(e.doc_ids))
        ]
        relations = [
            EvidenceRelation(r.src, r.tgt, r.keywords, r.description, sources)
            for r in retrieval.relations
            if (sources := visible_sources(r.doc_ids))
        ]
        ordered = [d for d in retrieved_ids if d in records] + expanded
        return QueryResult(
            chunks=chunks,
            entities=entities,
            relations=relations,
            sources=[Source(d, records[d].commit_sha) for d in ordered],
            mode=mode,
            branch=branch,
            no_evidence=not (chunks or entities or relations),
        )
