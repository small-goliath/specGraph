import pytest

from fakes import FakeLightRAG, InMemoryManifest
from specgraph.errors import InvalidArgumentError, SpecGraphError
from specgraph.indexer.extract import DocumentRef, extract_chapter
from specgraph.indexer.kg import build_chapter_kg
from specgraph.lightrag_store import Retrieval, RetrievedChunk, RetrievedEntity, RetrievedRelation
from specgraph.manifest import ChapterRecord
from specgraph.mcp_server.query_service import QueryResult, QueryService, Source

ADMIN = DocumentRef("draft/settlr-admin-prd", "prd/settlr-admin-prd.md")
PARTNER = DocumentRef("draft/settlr-partner-prd", "prd/settlr-partner-prd.md")
ADMIN5 = "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"
ADMIN3 = "draft/settlr-admin-prd:prd/settlr-admin-prd.md#3"
PARTNER2 = "draft/settlr-partner-prd:prd/settlr-partner-prd.md#2"
PARTNER1 = "draft/settlr-partner-prd:prd/settlr-partner-prd.md#1"


def _record(doc_id, text, sha):
    branch, rest = doc_id.split(":", 1)
    path, section = rest.rsplit("#", 1)
    refs = extract_chapter(text)
    payload = build_chapter_kg(doc_id, text.splitlines()[0], refs, [ADMIN, PARTNER])
    return ChapterRecord(
        doc_id=doc_id,
        branch=branch,
        path=path,
        section=section,
        title=text.splitlines()[0],
        content=text,
        content_hash="h",
        commit_sha=sha,
        content_commit_sha=sha,
        screens=refs.screen_ranks,
        policies=refs.policy_ranks,
        kg_keys=payload.keys,
    )


@pytest.fixture
def manifest():
    m = InMemoryManifest()
    for rec in [
        _record(ADMIN5, "## 5. 정산 정책\n정산 주기는 D+2 (P-14.3).", "sha-admin"),
        _record(ADMIN3, "## 3. ADM-03 정산 내역\n조회 조건은 §5.2.", "sha-admin"),
        _record(
            PARTNER2, "## 2. PTN-P-04 정산 조회\n정산 주기는 Admin PRD §5 를 따른다.", "sha-ptn"
        ),
        _record(PARTNER1, "## 1. 개요\n파트너 포털.", "sha-ptn"),
    ]:
        m.records[rec.doc_id] = rec
    return m


class FakeLlm:
    def __init__(self, answer="근거에 따른 답변"):
        self.prompts = []
        self.answer = answer

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.prompts.append((prompt, system_prompt))
        return self.answer


def _service(manifest, retrieval, llm=None):
    rag = FakeLightRAG(retrieval=retrieval)
    llm = llm or FakeLlm()
    return QueryService(rag=rag, manifest=manifest, llm=llm), rag, llm


def _partner_only_retrieval():
    return Retrieval(
        chunks=[RetrievedChunk(PARTNER2, "정산 주기는 Admin PRD §5 를 따른다.")],
        entities=[RetrievedEntity("정산 주기", "concept", "D+2 정산", (PARTNER2,))],
        relations=[],
    )


async def test_sources_include_doc_id_and_commit_sha(manifest):
    service, _, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 주기 근거는?", "mix", None)

    assert isinstance(result, QueryResult)
    assert result.answer == "근거에 따른 답변"
    assert Source(PARTNER2, "sha-ptn") in result.sources
    assert all(s.doc_id and s.commit_sha for s in result.sources)


async def test_cross_branch_reference_expansion_adds_admin_doc(manifest):
    service, _, llm = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 정책 근거는?", "mix", None)

    doc_ids = [s.doc_id for s in result.sources]
    assert doc_ids[0] == PARTNER2
    assert ADMIN5 in doc_ids
    assert Source(ADMIN5, "sha-admin") in result.sources
    prompt = llm.prompts[0][0]
    assert "정산 주기는 D+2 (P-14.3)." in prompt
    assert ADMIN5 in prompt and PARTNER2 in prompt


async def test_branch_filter_keeps_only_branch_doc_ids(manifest):
    retrieval = Retrieval(
        chunks=[
            RetrievedChunk(ADMIN3, "조회 조건은 §5.2."),
            RetrievedChunk(PARTNER1, "파트너 포털."),
        ],
        entities=[RetrievedEntity("파트너", "concept", "사업자", (PARTNER1, ADMIN3))],
        relations=[RetrievedRelation("a", "b", "k", "관계", (PARTNER1,))],
    )
    service, _, llm = _service(manifest, retrieval)

    result = await service.query("질문", "hybrid", "draft/settlr-admin-prd")

    assert result.sources
    assert all(s.doc_id.startswith("draft/settlr-admin-prd:") for s in result.sources)
    assert "파트너 포털." not in llm.prompts[0][0]
    assert "관계" not in llm.prompts[0][0]


async def test_branch_filter_blocks_reference_expansion_outside_branch(manifest):
    service, _, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("질문", "mix", "draft/settlr-partner-prd")

    assert [s.doc_id for s in result.sources] == [PARTNER2]


async def test_same_document_reference_expanded(manifest):
    retrieval = Retrieval(chunks=[RetrievedChunk(ADMIN3, "조회 조건은 §5.2.")])
    service, _, _ = _service(manifest, retrieval)

    result = await service.query("질문", "naive", "draft/settlr-admin-prd")

    assert [s.doc_id for s in result.sources] == [ADMIN3, ADMIN5]


async def test_no_sources_returns_without_llm_call(manifest):
    service, _, llm = _service(manifest, Retrieval())

    result = await service.query("질문", "mix", None)

    assert result.sources == []
    assert llm.prompts == []
    assert result.answer


async def test_retrieval_called_with_mode(manifest):
    service, rag, _ = _service(manifest, _partner_only_retrieval())

    await service.query("질문", "local", None)

    assert rag.calls[0] == ("retrieve", "질문", "local")


@pytest.mark.parametrize("mode", ["bypass", "graph", ""])
async def test_invalid_mode_rejected(manifest, mode):
    service, rag, _ = _service(manifest, _partner_only_retrieval())

    with pytest.raises(InvalidArgumentError) as exc:
        await service.query("질문", mode, None)
    assert isinstance(exc.value, SpecGraphError)
    assert rag.calls == []


async def test_empty_question_rejected(manifest):
    service, _, _ = _service(manifest, _partner_only_retrieval())

    with pytest.raises(InvalidArgumentError):
        await service.query("   ", "mix", None)


async def test_branch_filter_drops_entities_and_relations_mixed_with_other_branches(manifest):
    """C10: 다른 브랜치 출처가 섞인 엔티티 · 관계 설명은 브랜치 한정 질의 프롬프트에 넣지 않는다."""
    retrieval = Retrieval(
        chunks=[RetrievedChunk(ADMIN3, "조회 조건은 §5.2.")],
        entities=[
            RetrievedEntity("정산", "concept", "파트너 문서가 섞인 설명", (ADMIN3, PARTNER1)),
            RetrievedEntity("ADM-03", "SCREEN", "어드민 전용 설명", (ADMIN3,)),
        ],
        relations=[
            RetrievedRelation("정산", "파트너", "k", "섞인 관계", (ADMIN3, PARTNER2)),
            RetrievedRelation("ADM-03", "정산", "k", "어드민 관계", (ADMIN3,)),
        ],
    )
    service, _, llm = _service(manifest, retrieval)

    await service.query("질문", "mix", "draft/settlr-admin-prd")

    prompt = llm.prompts[0][0]
    assert "어드민 전용 설명" in prompt and "어드민 관계" in prompt
    assert "파트너 문서가 섞인 설명" not in prompt and "섞인 관계" not in prompt


async def test_result_to_dict_has_sources_with_commit_sha(manifest):
    service, _, _ = _service(manifest, _partner_only_retrieval())

    data = (await service.query("질문", "mix", None)).to_dict()

    assert data["answer"]
    assert data["sources"][0] == {"doc_id": PARTNER2, "commit_sha": "sha-ptn"}
