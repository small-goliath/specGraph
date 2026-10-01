"""질의 서비스 (AC15 · AC16 · AC17).

1. LightRAG 구조화 검색(``aquery_data`` — LLM 생성 없이 근거만)으로 청크 · 엔티티 · 관계를 얻는다.
2. branch 가 주어지면 doc_id 접두어(``<branch>:``)로 모든 근거를 거른다.
3. 검색된 챕터의 결정적 참조(REFERS_TO — "Admin PRD §5", "§5.2")를 한 단계 따라가
   참조 대상 챕터를 근거에 추가한다(branch 필터가 있으면 그 브랜치 안에서만).
4. 걸러진 근거만으로 LLM 이 한국어 답변을 만든다.
5. 모든 출처에 doc_id 와 manifest 의 commit_sha 를 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from specgraph.docid import belongs_to_branch, parse_doc_id
from specgraph.errors import InvalidArgumentError
from specgraph.lightrag_store import QUERY_MODES, LightRagPort, Retrieval
from specgraph.llm import LlmFunc
from specgraph.manifest import ChapterRecord, ManifestPort

NO_EVIDENCE_ANSWER = "질문과 관련된 근거 문서를 찾지 못했다."
EXPANDED_CHAPTER_CHARS = 4000

SYSTEM_PROMPT = (
    "너는 product-docs 기획 문서(PRD · UI/UX spec · tech-spec)에 대한 질문에 답하는 도우미다. "
    "아래 '근거'에 있는 내용만 사용해 한국어로 답하라. 근거에 없는 내용은 추측하지 말고 "
    "모른다고 답하라. 문장마다 근거의 doc_id 를 [doc_id] 형식으로 인용하라."
)


@dataclass(frozen=True)
class Source:
    doc_id: str
    commit_sha: str

    def to_dict(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "commit_sha": self.commit_sha}


@dataclass
class QueryResult:
    answer: str
    sources: list[Source] = field(default_factory=list)
    mode: str = "mix"
    branch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "mode": self.mode,
            "branch": self.branch,
            "sources": [s.to_dict() for s in self.sources],
        }


def _is_chapter_id(value: str) -> bool:
    try:
        parse_doc_id(value)
    except ValueError:
        return False
    return True


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
    def __init__(self, rag: LightRagPort, manifest: ManifestPort, llm: LlmFunc) -> None:
        self.rag = rag
        self.manifest = manifest
        self.llm = llm

    async def query(
        self, question: str, mode: str = "mix", branch: str | None = None
    ) -> QueryResult:
        if mode not in QUERY_MODES:
            raise InvalidArgumentError(
                f"mode 는 {', '.join(QUERY_MODES)} 중 하나여야 한다: {mode!r}"
            )
        if not question or not question.strip():
            raise InvalidArgumentError("question 이 비어 있다")
        branch = branch or None

        retrieval = _filter(await self.rag.retrieve(question, mode), branch)

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

        ordered = [d for d in retrieved_ids if d in records] + expanded
        sources = [Source(d, records[d].commit_sha) for d in ordered]
        if not sources:
            return QueryResult(answer=NO_EVIDENCE_ANSWER, sources=[], mode=mode, branch=branch)

        prompt = self._prompt(question, retrieval, ordered, expanded, records)
        answer = await self.llm(prompt, system_prompt=SYSTEM_PROMPT)
        return QueryResult(answer=answer, sources=sources, mode=mode, branch=branch)

    @staticmethod
    def _prompt(
        question: str,
        retrieval: Retrieval,
        ordered: list[str],
        expanded: list[str],
        records: dict[str, ChapterRecord],
    ) -> str:
        parts = ["## 근거"]
        for doc_id in ordered:
            if doc_id in expanded:
                body = records[doc_id].content[:EXPANDED_CHAPTER_CHARS]
            else:
                body = "\n\n".join(c.content for c in retrieval.chunks if c.doc_id == doc_id)
                body = body or records[doc_id].content[:EXPANDED_CHAPTER_CHARS]
            parts.append(f"### [{doc_id}] {records[doc_id].title}\n{body}")
        if retrieval.entities:
            parts.append("## 관련 엔티티")
            parts.extend(
                f"- {e.name} ({e.entity_type}): {e.description}" for e in retrieval.entities
            )
        if retrieval.relations:
            parts.append("## 관련 관계")
            parts.extend(
                f"- {r.src} → {r.tgt} [{r.keywords}]: {r.description}" for r in retrieval.relations
            )
        parts.append(f"## 질문\n{question}")
        return "\n\n".join(parts)
