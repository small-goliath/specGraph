import pytest

from fakes import FakeLightRAG, InMemoryManifest
from specgraph.errors import InvalidArgumentError, SpecGraphError
from specgraph.indexer.extract import DocumentRef, extract_chapter
from specgraph.indexer.kg import build_chapter_kg
from specgraph.lightrag_store import Retrieval, RetrievedChunk, RetrievedEntity, RetrievedRelation
from specgraph.llm import LlmCallCounter
from specgraph.manifest import ChapterRecord
from specgraph.mcp_server.query_service import EvidenceChunk, QueryResult, QueryService, Source

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


def _service(manifest, retrieval, counter=None):
    rag = FakeLightRAG(retrieval=retrieval, counter=counter)
    return QueryService(rag=rag, manifest=manifest), rag


def _partner_only_retrieval():
    return Retrieval(
        chunks=[RetrievedChunk(PARTNER2, "정산 주기는 Admin PRD §5 를 따른다.")],
        entities=[RetrievedEntity("정산 주기", "concept", "D+2 정산", (PARTNER2,))],
        relations=[],
    )


async def test_query_service_requires_no_llm(manifest):
    """PPS-350 AC17: 질의 서비스는 LLM 없이 만들어지고 동작한다."""
    service = QueryService(FakeLightRAG(retrieval=_partner_only_retrieval()), manifest)

    result = await service.query("파트너 정산 주기 근거는?", "mix", None)

    assert isinstance(result, QueryResult)
    assert Source(PARTNER2, "sha-ptn") in result.sources


async def test_sources_include_doc_id_and_commit_sha(manifest):
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 주기 근거는?", "mix", None)

    assert Source(PARTNER2, "sha-ptn") in result.sources
    assert all(s.doc_id and s.commit_sha for s in result.sources)


async def test_result_has_no_answer_and_chunks_carry_doc_id_and_commit_sha(manifest):
    """PPS-350 AC14: answer 가 없고 청크 본문이 doc_id · commit_sha 와 함께 돌아온다."""
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 주기 근거는?", "mix", None)

    assert not hasattr(result, "answer")
    assert result.chunks[0] == EvidenceChunk(
        doc_id=PARTNER2,
        commit_sha="sha-ptn",
        content="정산 주기는 Admin PRD §5 를 따른다.",
        expanded=False,
    )
    assert result.no_evidence is False


async def test_entities_and_relations_carry_visible_sources_with_commit_sha(manifest):
    retrieval = Retrieval(
        chunks=[],
        entities=[RetrievedEntity("정산 주기", "concept", "D+2 정산", (PARTNER2, ADMIN5))],
        relations=[RetrievedRelation("PTN-P-04", "정산 주기", "k", "화면이 보여준다", (PARTNER2,))],
    )
    service, _ = _service(manifest, retrieval)

    result = await service.query("정산 주기?", "mix", None)

    entity = result.entities[0]
    assert (entity.name, entity.entity_type, entity.description) == (
        "정산 주기",
        "concept",
        "D+2 정산",
    )
    assert entity.sources == (Source(PARTNER2, "sha-ptn"), Source(ADMIN5, "sha-admin"))
    relation = result.relations[0]
    assert (relation.src, relation.tgt, relation.keywords) == ("PTN-P-04", "정산 주기", "k")
    assert relation.sources == (Source(PARTNER2, "sha-ptn"),)
    data = result.to_dict()
    assert data["entities"][0]["sources"][1] == {"doc_id": ADMIN5, "commit_sha": "sha-admin"}
    assert data["relations"][0]["description"] == "화면이 보여준다"


async def test_entity_with_only_hidden_sources_is_dropped(manifest):
    """PPS-350 AC15: 보이는 출처가 하나도 없는 엔티티 · 관계 · 청크는 응답에서 뺀다."""
    from dataclasses import replace

    manifest.records[ADMIN5] = replace(
        manifest.records[ADMIN5], content_hash="", content_commit_sha=""
    )
    retrieval = Retrieval(
        chunks=[
            RetrievedChunk(ADMIN5, "숨겨진 챕터 본문"),
            RetrievedChunk(PARTNER2, "보이는 본문"),
        ],
        entities=[
            RetrievedEntity("숨김만", "concept", "숨겨진 출처만", (ADMIN5,)),
            RetrievedEntity("섞임", "concept", "보이는 출처 포함", (ADMIN5, PARTNER2)),
        ],
        relations=[RetrievedRelation("a", "b", "k", "숨겨진 관계", (ADMIN5,))],
    )
    service, _ = _service(manifest, retrieval)

    result = await service.query("질문", "mix", None)

    assert [c.doc_id for c in result.chunks] == [PARTNER2]
    assert [e.name for e in result.entities] == ["섞임"]
    assert result.entities[0].sources == (Source(PARTNER2, "sha-ptn"),)
    assert result.relations == []
    assert "숨겨진" not in str(result.to_dict())


async def test_expanded_reference_chapter_is_returned_as_chunk(manifest):
    """참조 확장(REFERS_TO 한 단계)으로 추가된 챕터는 expanded=True 청크로 돌아온다."""
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 정책 근거는?", "mix", None)

    assert [s.doc_id for s in result.sources] == [PARTNER2, ADMIN5]
    assert Source(ADMIN5, "sha-admin") in result.sources
    expanded = [c for c in result.chunks if c.expanded]
    assert [c.doc_id for c in expanded] == [ADMIN5]
    assert expanded[0].commit_sha == "sha-admin"
    assert "정산 주기는 D+2 (P-14.3)." in expanded[0].content
    assert result.chunks[0].doc_id == PARTNER2 and result.chunks[0].expanded is False


async def test_branch_filter_keeps_only_branch_doc_ids(manifest):
    retrieval = Retrieval(
        chunks=[
            RetrievedChunk(ADMIN3, "조회 조건은 §5.2."),
            RetrievedChunk(PARTNER1, "파트너 포털."),
        ],
        entities=[RetrievedEntity("파트너", "concept", "사업자", (PARTNER1, ADMIN3))],
        relations=[RetrievedRelation("a", "b", "k", "관계", (PARTNER1,))],
    )
    service, _ = _service(manifest, retrieval)

    result = await service.query("질문", "hybrid", "draft/settlr-admin-prd")

    assert result.sources
    assert all(s.doc_id.startswith("draft/settlr-admin-prd:") for s in result.sources)
    assert [c.content for c in result.chunks if not c.expanded] == ["조회 조건은 §5.2."]
    assert result.entities == [] and result.relations == []


async def test_branch_filter_blocks_reference_expansion_outside_branch(manifest):
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("질문", "mix", "draft/settlr-partner-prd")

    assert [s.doc_id for s in result.sources] == [PARTNER2]


async def test_same_document_reference_expanded(manifest):
    retrieval = Retrieval(chunks=[RetrievedChunk(ADMIN3, "조회 조건은 §5.2.")])
    service, _ = _service(manifest, retrieval)

    result = await service.query("질문", "naive", "draft/settlr-admin-prd")

    assert [s.doc_id for s in result.sources] == [ADMIN3, ADMIN5]


async def test_no_evidence_returns_empty_lists_and_flag_without_error(manifest):
    """PPS-350 AC16: 근거가 없으면 빈 목록 + no_evidence=True 로 정상 반환한다."""
    service, _ = _service(manifest, Retrieval())

    result = await service.query("질문", "mix", None)

    assert result.chunks == [] and result.entities == [] and result.relations == []
    assert result.sources == []
    assert result.no_evidence is True
    data = result.to_dict()
    assert data["no_evidence"] is True and "answer" not in data


async def test_retrieval_called_with_mode(manifest):
    service, rag = _service(manifest, _partner_only_retrieval())

    await service.query("질문", "local", None)

    assert rag.calls[0] == ("retrieve", "질문", "local", (), ())


async def test_query_forwards_keywords_to_retrieval(manifest):
    """PPS-350 AC11: 공백 · 빈 항목을 정리한 키워드가 그대로 어댑터로 간다."""
    service, rag = _service(manifest, _partner_only_retrieval())

    await service.query(
        "질문", "mix", None, hl_keywords=[" 정산 정책 ", ""], ll_keywords=["P-14.3", "  "]
    )

    assert rag.calls[0] == ("retrieve", "질문", "mix", ("정산 정책",), ("P-14.3",))


async def test_query_without_keywords_passes_empty_lists_to_adapter(manifest):
    """키워드 생략 시 질의문 보정은 어댑터(lightrag_store) 가 한다 — 서비스는 비운 채로 넘긴다."""
    service, rag = _service(manifest, _partner_only_retrieval())

    await service.query("질문", "mix", None)

    assert rag.calls[0][3] == () and rag.calls[0][4] == ()


async def test_query_makes_no_llm_calls(manifest):
    """PPS-350 AC12: 질의 경로의 LLM 호출은 0 건이다."""
    counter = LlmCallCounter()
    service, _ = _service(manifest, _partner_only_retrieval(), counter=counter)

    await service.query("질문", "mix", None, ll_keywords=["정산"])
    await service.query("질문", "mix", None)

    assert counter.total == 0


@pytest.mark.parametrize("mode", ["bypass", "graph", ""])
async def test_invalid_mode_rejected(manifest, mode):
    service, rag = _service(manifest, _partner_only_retrieval())

    with pytest.raises(InvalidArgumentError) as exc:
        await service.query("질문", mode, None)
    assert isinstance(exc.value, SpecGraphError)
    assert rag.calls == []


async def test_empty_question_rejected(manifest):
    service, _ = _service(manifest, _partner_only_retrieval())

    with pytest.raises(InvalidArgumentError):
        await service.query("   ", "mix", None)


async def test_branch_filter_drops_entities_and_relations_mixed_with_other_branches(manifest):
    """C10: 다른 브랜치 출처가 섞인 엔티티 · 관계 설명은 브랜치 한정 질의 응답에 넣지 않는다."""
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
    service, _ = _service(manifest, retrieval)

    result = await service.query("질문", "mix", "draft/settlr-admin-prd")

    assert [e.description for e in result.entities] == ["어드민 전용 설명"]
    assert [r.description for r in result.relations] == ["어드민 관계"]


async def test_result_to_dict_has_sources_with_commit_sha(manifest):
    service, _ = _service(manifest, _partner_only_retrieval())

    data = (await service.query("질문", "mix", None)).to_dict()

    assert "answer" not in data
    assert data["sources"][0] == {"doc_id": PARTNER2, "commit_sha": "sha-ptn"}
    assert data["chunks"][0] == {
        "doc_id": PARTNER2,
        "commit_sha": "sha-ptn",
        "content": "정산 주기는 Admin PRD §5 를 따른다.",
        "expanded": False,
    }
    assert data["mode"] == "mix" and data["branch"] is None and data["no_evidence"] is False


def _pending_record(doc_id, text, *, commit_sha, content_commit_sha):
    """삽입 시작 기록(content_hash 가 빈 PENDING). content_commit_sha 가 비면 첫 삽입이다."""
    from dataclasses import replace

    base = _record(doc_id, text, commit_sha)
    return replace(
        base,
        doc_id=doc_id,
        content_hash="",
        commit_sha=commit_sha,
        content_commit_sha=content_commit_sha,
    )


async def test_query_excludes_pending_record_from_sources(manifest):
    manifest.records[PARTNER2] = _pending_record(
        PARTNER2, "## 2. PTN-P-04 정산 조회\n새 본문", commit_sha="sha-new", content_commit_sha=""
    )
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 주기 근거는?", "mix", None)

    assert PARTNER2 not in [s.doc_id for s in result.sources]
    assert "sha-new" not in str(result.to_dict())


async def test_query_source_sha_uses_completed_commit_during_reinsert(manifest):
    """AC3: 재삽입 중 출처 SHA 는 새 삽입이 끝나기 전 완료본의 SHA 다."""
    manifest.records[PARTNER2] = _pending_record(
        PARTNER2, "## 2. 직전 완료본", commit_sha="sha-done", content_commit_sha="sha-done"
    )
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 주기 근거는?", "mix", None)

    assert Source(PARTNER2, "sha-done") in result.sources


async def test_query_reference_expansion_ignores_pending_kg_keys(manifest):
    """AC4: PENDING 레코드의 kg_keys(옛 · 새 시도의 관계를 합친 값)로 참조를 확장하지 않는다."""
    from dataclasses import replace

    from specgraph.kg_model import KgKeys

    # PARTNER2 는 재삽입 중(보이는 PENDING) — kg_keys 에 ADMIN5 관계가 있어도 따라가지 않는다.
    pending = _pending_record(
        PARTNER2,
        "## 2. PTN-P-04 정산 조회\n직전 본문",
        commit_sha="sha-done",
        content_commit_sha="sha-done",
    )
    manifest.records[PARTNER2] = replace(
        pending, kg_keys=KgKeys(PARTNER2, ((PARTNER2, ADMIN5),), "anchor")
    )
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 정책 근거는?", "mix", None)

    assert [s.doc_id for s in result.sources] == [PARTNER2]


async def test_query_reference_expansion_skips_hidden_target(manifest):
    from dataclasses import replace

    manifest.records[ADMIN5] = replace(
        manifest.records[ADMIN5], content_hash="", content_commit_sha=""
    )
    service, _ = _service(manifest, _partner_only_retrieval())

    result = await service.query("파트너 정산 정책 근거는?", "mix", None)

    assert ADMIN5 not in [s.doc_id for s in result.sources]
