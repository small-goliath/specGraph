"""LightRAG 어댑터 — 순수 변환 함수 + 실제 LightRAG(로컬 파일 스토리지, 가짜 LLM · 임베딩) 왕복.

외부 서비스 없이 설치된 lightrag-hku 의 실제 API(ainsert ids/file_paths, ainsert_custom_kg,
adelete_by_doc_id, adelete_by_entity/relation, amerge_entities, aquery_data)를 검증한다.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from specgraph.blocks import BLOCK_SEPARATOR
from specgraph.indexer.extract import DocumentRef, extract_chapter
from specgraph.indexer.kg import build_chapter_kg
from specgraph.lightrag_store import (
    LightRagStore,
    build_insert_body,
    decode_sources,
    encode_payload,
    encode_source,
    make_embedding_func,
    make_rerank_func,
    to_retrieval,
)
from specgraph.llm import LlmCallCounter

DOC = "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"
OTHER = "draft/settlr-partner-prd:prd/settlr-partner-prd.md#2"


def test_insert_body_header_is_unique_per_doc_and_never_spells_the_doc_id():
    """R2-C1: 머리 줄이 LLM 에 doc_id 엔티티로 추출돼 CHAPTER 엔티티와 충돌하지 않게 한다."""
    blocks = ["## 9. 변경 이력", "| 버전 | 일자 |\n|---|---|\n| 0.1 | 2026-09-01 |"]

    mine = build_insert_body(DOC, blocks, limit=1000, measure=len)
    other = build_insert_body(OTHER, blocks, limit=1000, measure=len)

    assert mine.text != other.text
    for doc_id, body in ((DOC, mine), (OTHER, other)):
        assert doc_id not in body.text
        assert "settlr" not in body.text and "draft/" not in body.text
    assert mine.text.split("\n", 1)[1] == BLOCK_SEPARATOR.join(blocks)
    assert mine.oversized == []


def test_insert_body_refits_blocks_by_measure_and_reports_oversized():
    rows = "\n".join(f"| P-{i} | {'x' * 20} |" for i in range(10))
    table = "| ID | 내용 |\n|---|---|\n" + rows
    single = "y" * 500

    body = build_insert_body(DOC, [table, single], limit=200, measure=len)

    pieces = body.text.split(BLOCK_SEPARATOR)
    assert len(pieces) > 2
    assert all(p.count("| ID | 내용 |") == 1 for p in pieces if "| P-" in p)
    assert body.oversized == [500]


class _StubRag:
    """insert_chapter 가 LightRAG 에 넘기는 인자를 기록하는 최소 스텁."""

    chunk_token_size = 1000

    class tokenizer:  # noqa: N801 — LightRAG 속성 이름
        @staticmethod
        def encode(text: str) -> list[str]:
            return list(text)

    def __init__(self) -> None:
        self.inserted: list[tuple[str, dict]] = []

    async def ainsert(self, text, **kwargs):
        self.inserted.append((text, kwargs))

    async def aget_docs_by_ids(self, ids):
        return {i: {"status": "processed"} for i in ids}


async def test_insert_chapter_sends_exactly_build_insert_body():
    """R2-A1: 어댑터가 넘기는 본문과 순수 함수의 본문이 같다(조립 로직이 한 곳)."""
    rag = _StubRag()
    blocks = ["## 5. 정산 정책", "| 정책 ID | 내용 |\n|---|---|\n| P-14.3 | D+2 |"]

    await LightRagStore(rag).insert_chapter(DOC, blocks)

    text, kwargs = rag.inserted[0]
    assert text == build_insert_body(DOC, blocks, limit=1000, measure=len).text
    assert kwargs["ids"] == [DOC] and kwargs["split_by_character_only"] is True


def test_encode_source_has_no_path_separator_and_roundtrips():
    encoded = encode_source(DOC)

    assert "/" not in encoded and ":" not in encoded and "#" not in encoded
    assert decode_sources(encoded) == [DOC]


def test_decode_sources_handles_multiple_and_ignores_unknown():
    raw = f"{encode_source(DOC)}<SEP>custom_kg<SEP>{encode_source(OTHER)}<SEP>{encode_source(DOC)}"

    assert decode_sources(raw) == [DOC, OTHER]
    assert decode_sources(None) == []


def test_to_retrieval_maps_file_paths_to_doc_ids():
    data = {
        "status": "success",
        "data": {
            "chunks": [
                {"content": "본문", "file_path": encode_source(DOC)},
                {"content": "출처 없음", "file_path": "unknown_source"},
            ],
            "entities": [
                {
                    "entity_name": "정산",
                    "entity_type": "concept",
                    "description": "d",
                    "file_path": f"{encode_source(DOC)}<SEP>{encode_source(OTHER)}",
                }
            ],
            "relationships": [
                {
                    "src_id": "a",
                    "tgt_id": "b",
                    "keywords": "k",
                    "description": "d",
                    "file_path": encode_source(OTHER),
                }
            ],
        },
    }

    retrieval = to_retrieval(data)

    assert [(c.doc_id, c.content) for c in retrieval.chunks] == [(DOC, "본문")]
    assert retrieval.entities[0].doc_ids == (DOC, OTHER)
    assert retrieval.relations[0].doc_ids == (OTHER,)


def test_factories_use_settings_only(settings_env):
    from specgraph.settings import load_settings

    settings_env.setenv("EMBEDDING_MODEL", "embed-from-env")
    settings_env.setenv("EMBEDDING_DIM", "512")
    settings_env.setenv("EMBEDDING_BINDING_HOST", "http://embed:1")
    settings_env.setenv("RERANK_MODEL", "rerank-from-env")
    settings_env.setenv("RERANK_BINDING_HOST", "http://rerank:2/rerank")
    s = load_settings()

    embed = make_embedding_func(s)
    rerank = make_rerank_func(s)

    assert embed.embedding_dim == 512
    assert embed.model_name == "embed-from-env"
    assert embed.func.keywords == {"embed_model": "embed-from-env", "host": "http://embed:1"}
    assert rerank.keywords["model"] == "rerank-from-env"
    # LightRAG 서버와 같은 의미: RERANK_BINDING_HOST 는 rerank 엔드포인트 전체 URL
    assert rerank.keywords["base_url"] == "http://rerank:2/rerank"


def test_openai_compatible_embedding_binding_uses_local_host(settings_env):
    from specgraph.settings import load_settings

    settings_env.setenv("EMBEDDING_BINDING", "openai")
    settings_env.setenv("EMBEDDING_BINDING_HOST", "http://infinity:7997")

    embed = make_embedding_func(load_settings())

    assert embed.func.keywords["base_url"] == "http://infinity:7997"
    assert embed.func.keywords["model"] == "test-embed"


def test_rerank_disabled_without_model(settings):
    assert make_rerank_func(settings) is None


def test_rerank_requires_host_when_model_set(settings_env):
    from specgraph.errors import ConfigError
    from specgraph.settings import load_settings

    settings_env.setenv("RERANK_MODEL", "r")

    with pytest.raises(ConfigError):
        make_rerank_func(load_settings())


def test_to_retrieval_failure_response_is_empty():
    retrieval = to_retrieval({"status": "failure", "message": "x", "data": {}})

    assert retrieval.chunks == [] and retrieval.entities == [] and retrieval.relations == []


def test_encode_payload_encodes_every_file_path_without_mutating_input():
    payload = build_chapter_kg(
        OTHER,
        "2. PTN-P-04",
        extract_chapter("## 2. PTN-P-04\nAdmin PRD §5"),
        [DocumentRef("draft/settlr-admin-prd", "prd/settlr-admin-prd.md")],
    )

    encoded = encode_payload(payload)

    for key in ("chunks", "entities", "relationships"):
        assert all(item["file_path"] == encode_source(OTHER) for item in encoded[key])
    assert payload.custom_kg["chunks"][0]["file_path"] == OTHER


# --- 실제 LightRAG (로컬 스토리지) ------------------------------------------------


class CharTokenizer:
    """tiktoken 다운로드 없이 쓰는 문자 단위 토크나이저."""

    def encode(self, content: str) -> list[int]:
        return [ord(ch) for ch in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(t) for t in tokens)


async def _fake_embed(texts: list[str], **_: object) -> np.ndarray:
    vectors = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vectors.append(np.frombuffer(digest[:16], dtype=np.uint8).astype(np.float32) + 1.0)
    return np.array(vectors)


async def _fake_llm(prompt: str, system_prompt: str | None = None, **_: object) -> str:
    return ""


def _workspace(path) -> str:
    """LightRAG 는 Json 스토리지 데이터를 프로세스 공유 메모리에 workspace 단위로 둔다.
    테스트끼리 섞이지 않도록 테스트마다 다른 workspace 를 쓴다."""
    return "t" + hashlib.sha256(str(path).encode()).hexdigest()[:12]


@pytest.fixture
async def store(settings_env, tmp_path):
    from lightrag.utils import EmbeddingFunc, Tokenizer

    from specgraph.settings import load_settings

    settings_env.setenv("SPECGRAPH_WORKING_DIR", str(tmp_path / "rag"))
    settings_env.setenv("LIGHTRAG_KV_STORAGE", "JsonKVStorage")
    settings_env.setenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage")
    settings_env.setenv("LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage")
    settings_env.setenv("LIGHTRAG_DOC_STATUS_STORAGE", "JsonDocStatusStorage")
    s = load_settings()
    created = await LightRagStore.create(
        s,
        LlmCallCounter(),
        llm_model_func=_fake_llm,
        embedding_func=EmbeddingFunc(embedding_dim=16, func=_fake_embed, model_name="fake"),
        tokenizer=Tokenizer("char", CharTokenizer()),
        rerank_model_func=None,
        workspace=_workspace(tmp_path),
    )
    yield created
    await created.close()


async def test_real_lightrag_insert_and_delete_chapter_by_doc_id(store):
    await store.insert_chapter(
        DOC, ["## 5. 정산 정책", "| 정책 ID | 내용 |\n|---|---|\n| P-14.3 | D+2 |"]
    )

    statuses = await store.rag.aget_docs_by_ids([DOC])
    assert DOC in statuses

    retrieval = await store.retrieve("정산 정책 P-14.3", "naive")
    assert {c.doc_id for c in retrieval.chunks} == {DOC}

    await store.delete_chapter(DOC)
    assert await store.rag.aget_docs_by_ids([DOC]) == {}


async def test_real_lightrag_custom_kg_upsert_delete_and_merge(store):
    admin = DocumentRef("draft/settlr-admin-prd", "prd/settlr-admin-prd.md")
    payload = build_chapter_kg(
        OTHER,
        "2. PTN-P-04 정산 조회",
        extract_chapter("## 2. PTN-P-04 정산 조회\nPTN-P-04 화면. Admin PRD §5 (P-14.3)"),
        [admin],
    )

    await store.upsert_chapter_kg(payload, previous=None)

    names = set(await store.entity_names())
    assert {OTHER, "PTN-P-04", "P-14.3", DOC}.issubset(names)
    graph = store.rag.chunk_entity_relation_graph
    assert await graph.has_edge(OTHER, DOC)
    node = await graph.get_node("PTN-P-04")
    assert decode_sources(node["file_path"]) == [OTHER]

    await store.upsert_chapter_kg(payload, previous=payload.keys)
    assert await graph.has_edge(OTHER, "PTN-P-04")

    await store.delete_chapter_kg(payload.keys)
    assert not await graph.has_node(OTHER)
    assert not await graph.has_edge(OTHER, DOC)

    await store.delete_entities(["PTN-P-04"])
    assert not await graph.has_node("PTN-P-04")

    await store.merge_entities(["P-14.3"], "정책 P-14.3")
    names = set(await store.entity_names())
    assert "정책 P-14.3" in names and "P-14.3" not in names


async def test_real_lightrag_identical_bodies_with_different_doc_ids_both_processed(store):
    blocks = ["## 9. 변경 이력", "| 버전 | 일자 |\n|---|---|\n| 0.1 | 2026-09-01 |"]

    await store.insert_chapter(DOC, blocks)
    await store.insert_chapter(OTHER, blocks)

    statuses = await store.rag.aget_docs_by_ids([DOC, OTHER])
    assert set(statuses) == {DOC, OTHER}


async def test_real_lightrag_failed_insert_stays_failed_until_deleted(settings_env, tmp_path):
    """R2-T3: 가짜(FakeLightRAG)의 전제를 실제로 확인한다 — 같은 본문 재삽입은 지우기 전까지
    FAILED 그대로이고, 지운 뒤 같은 본문을 넣으면 처리된다."""
    from lightrag.utils import Tokenizer

    from specgraph.errors import IndexingError

    failures = {"left": 1}

    async def flaky_llm(prompt: str, system_prompt: str | None = None, **_: object) -> str:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("일시 오류")
        return ""

    blocks = ["## 5. 정산 정책\n정산 주기는 D+2 이다."]
    real = await _create_store(
        settings_env,
        tmp_path / "rag-flaky",
        llm_model_func=flaky_llm,
        tokenizer=Tokenizer("char", CharTokenizer()),
    )
    try:
        with pytest.raises(IndexingError, match="status=failed"):
            await real.insert_chapter(DOC, blocks)

        with pytest.raises(IndexingError, match="status=failed"):
            await real.insert_chapter(DOC, blocks)  # 지우지 않고 재시도 → 무시되고 FAILED 유지

        await real.delete_chapter(DOC)
        await real.insert_chapter(DOC, blocks)

        statuses = await real.rag.aget_docs_by_ids([DOC])
        assert DOC in statuses
    finally:
        await real.close()


class ByteTokenizer:
    """UTF-8 바이트 단위 토크나이저 — 한글 1자가 3토큰이라 문자 수 한도를 넘는 표를 재현한다."""

    def encode(self, content: str) -> list[int]:
        return list(content.encode("utf-8"))

    def decode(self, tokens: list[int]) -> str:
        return bytes(tokens).decode("utf-8", "replace")


async def _create_store(settings_env, working_dir, **overrides):
    from lightrag.utils import EmbeddingFunc

    from specgraph.settings import load_settings

    settings_env.setenv("SPECGRAPH_WORKING_DIR", str(working_dir))
    settings_env.setenv("LIGHTRAG_KV_STORAGE", "JsonKVStorage")
    settings_env.setenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage")
    settings_env.setenv("LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage")
    settings_env.setenv("LIGHTRAG_DOC_STATUS_STORAGE", "JsonDocStatusStorage")
    kwargs = {
        "workspace": _workspace(working_dir),
        "llm_model_func": _fake_llm,
        "embedding_func": EmbeddingFunc(embedding_dim=16, func=_fake_embed, model_name="fake"),
        "rerank_model_func": None,
        **overrides,
    }
    counter = kwargs.pop("counter", LlmCallCounter())
    return await LightRagStore.create(load_settings(), counter, **kwargs)


async def _stored_chunks(store, doc_id):
    status = await store.rag.doc_status.get_by_id(doc_id)
    ids = status.get("chunks_list") or []
    return [c["content"] for c in await store.rag.text_chunks.get_by_ids(ids) if c]


async def test_real_lightrag_korean_table_chunks_are_never_cut_mid_row(settings_env, tmp_path):
    from lightrag.utils import Tokenizer

    from specgraph.blocks import split_blocks

    header = "| 정책 ID | 내용 |\n|---|---|"
    rows = [f"| P-{i}.1 | {'정산 주기와 수수료 정책 설명 ' * 2}|" for i in range(60)]
    text = "## 5. 정산 정책\n\n" + header + "\n" + "\n".join(rows)
    blocks = split_blocks(text, max_chars=1000)  # 문자 기준(서비스 기본값) — 토큰으로는 한도 초과
    tokenizer = ByteTokenizer()
    assert max(len(tokenizer.encode(b)) for b in blocks) > 1200

    real = await _create_store(
        settings_env, tmp_path / "rag-table", tokenizer=Tokenizer("bytes", tokenizer)
    )
    try:
        await real.insert_chapter(DOC, blocks)
        chunks = await _stored_chunks(real, DOC)
        limit = real.rag.chunk_token_size
    finally:
        await real.close()

    assert len(chunks) > 1
    seen = []
    for chunk in chunks:
        assert len(tokenizer.encode(chunk)) <= limit
        table_lines = [ln for ln in chunk.splitlines() if ln.lstrip().startswith("|")]
        assert all(ln.rstrip().endswith("|") for ln in table_lines)
        body = [ln for ln in table_lines if ln in rows]
        if body:
            assert header in chunk
        seen.extend(body)
    assert seen == rows


async def test_real_lightrag_oversized_single_row_warns_and_still_indexes(
    settings_env, tmp_path, caplog
):
    import logging

    from lightrag.utils import Tokenizer

    caplog.set_level(logging.WARNING)
    row = "| P-1.1 | " + "아주 긴 정책 설명 " * 80 + "|"
    blocks = ["## 5. 정산 정책", "| 정책 ID | 내용 |\n|---|---|\n" + row]

    real = await _create_store(
        settings_env, tmp_path / "rag-row", tokenizer=Tokenizer("bytes", ByteTokenizer())
    )
    try:
        await real.insert_chapter(DOC, blocks)
        statuses = await real.rag.aget_docs_by_ids([DOC])
    finally:
        await real.close()

    assert DOC in statuses
    warnings = [
        r.getMessage() for r in caplog.records if "event=block_over_token_limit" in r.getMessage()
    ]
    assert warnings and "doc_id=" in warnings[0]


async def test_real_lightrag_custom_kg_and_merge_make_no_llm_calls(settings_env, tmp_path):
    from lightrag.utils import Tokenizer

    from specgraph.llm import make_llm_func
    from specgraph.settings import load_settings

    class Client:
        async def chat(self, **_: object) -> dict:
            return {"message": {"content": ""}}

    counter = LlmCallCounter()
    real = await _create_store(
        settings_env,
        tmp_path / "rag-kg",
        counter=counter,
        llm_model_func=make_llm_func(load_settings(), counter, client_factory=Client),
        tokenizer=Tokenizer("char", CharTokenizer()),
    )
    admin = DocumentRef("draft/settlr-admin-prd", "prd/settlr-admin-prd.md")
    payload = build_chapter_kg(
        OTHER,
        "2. PTN-P-04 정산 조회",
        extract_chapter("## 2. PTN-P-04 정산 조회\nPTN-P-04 화면. Admin PRD §5 (P-14.3, U-2)"),
        [admin],
    )
    try:
        await real.upsert_chapter_kg(payload, previous=None)
        await real.upsert_chapter_kg(payload, previous=payload.keys)
        await real.merge_entities(["U-2"], "P-14.3")
        await real.delete_chapter_kg(payload.keys)
        await real.delete_entities(["PTN-P-04"])
    finally:
        await real.close()

    assert counter.total == 0


async def test_real_lightrag_llm_calls_attributed_to_doc_id(settings_env, tmp_path):
    from lightrag.utils import Tokenizer

    from specgraph.llm import make_llm_func
    from specgraph.settings import load_settings

    class Client:
        async def chat(self, **_: object) -> dict:
            return {"message": {"content": ""}}

    counter = LlmCallCounter()
    real = await _create_store(
        settings_env,
        tmp_path / "rag2",
        counter=counter,
        llm_model_func=make_llm_func(load_settings(), counter, client_factory=Client),
        tokenizer=Tokenizer("char", CharTokenizer()),
    )
    try:
        with counter.attribute_to(DOC):
            await real.insert_chapter(DOC, ["## 5. 정산 정책\n정산 주기는 D+2 이다."])
    finally:
        await real.close()

    assert counter.count_for(DOC) > 0
    assert counter.unattributed == 0


ADMIN_DOC = DocumentRef("draft/settlr-admin-prd", "prd/settlr-admin-prd.md")
PARTNER3 = "draft/settlr-partner-prd:prd/settlr-partner-prd.md#3"


def _payload(doc_id, text, documents=(ADMIN_DOC,)):
    return build_chapter_kg(doc_id, text.splitlines()[0], extract_chapter(text), list(documents))


def _sep(value):
    return [p for p in (value or "").split("<SEP>") if p]


async def test_real_lightrag_shared_screen_node_is_merged_not_overwritten(store):
    """C3: 공유 SCREEN 노드의 기존(LLM 추출) 설명 · 출처를 덮어쓰지 않고 합친다."""
    graph = store.rag.chunk_entity_relation_graph
    await graph.upsert_node(
        "PTN-P-04",
        {
            "entity_id": "PTN-P-04",
            "entity_type": "UNKNOWN",
            "description": "LLM 이 추출한 파트너 정산 조회 화면",
            "source_id": "chunk-llm",
            "file_path": encode_source(DOC),
        },
    )
    first = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면")
    second = _payload(PARTNER3, "## 3. 이력\nPTN-P-04 에서 이동")

    await store.upsert_chapter_kg(first, previous=None)
    await store.upsert_chapter_kg(second, previous=None)

    node = await graph.get_node("PTN-P-04")
    assert "LLM 이 추출한 파트너 정산 조회 화면" in node["description"]
    sources = _sep(node["source_id"])
    assert "chunk-llm" in sources and len(sources) == 3
    assert set(decode_sources(node["file_path"])) == {DOC, OTHER, PARTNER3}
    assert node["entity_type"] == "SCREEN"


async def test_real_lightrag_reinserted_chapter_kg_detaches_from_shared_nodes(store):
    """C3: 챕터 KG 를 지우면 공유 노드에서 그 챕터의 출처만 빠지고 노드는 남는다."""
    graph = store.rag.chunk_entity_relation_graph
    first = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면")
    second = _payload(PARTNER3, "## 3. 이력\nPTN-P-04 에서 이동")
    await store.upsert_chapter_kg(first, previous=None)
    await store.upsert_chapter_kg(second, previous=None)

    await store.delete_chapter_kg(first.keys)

    node = await graph.get_node("PTN-P-04")
    assert decode_sources(node["file_path"]) == [PARTNER3]
    assert len(_sep(node["source_id"])) == 1


async def test_real_lightrag_reinsert_kg_removes_previous_anchor_chunk(store):
    """C8: 재삽입으로 앵커 내용이 바뀌면 이전 앵커 청크를 지운다."""
    from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding

    before = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면")
    after = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-05 화면")
    assert before.keys.anchor_content != after.keys.anchor_content
    await store.upsert_chapter_kg(before, previous=None)

    await store.upsert_chapter_kg(after, previous=before.keys)

    def chunk_id(content):
        return compute_mdhash_id(sanitize_text_for_encoding(content), prefix="chunk-")

    assert await store.rag.text_chunks.get_by_id(chunk_id(before.keys.anchor_content)) is None
    assert await store.rag.text_chunks.get_by_id(chunk_id(after.keys.anchor_content)) is not None


async def test_real_lightrag_kg_upsert_keeps_edges_owned_by_other_chapters(store):
    """C9: LightRAG 엣지는 무방향이라, 상대 챕터가 소유한 같은 엣지는 지우지 않는다."""
    a = "draft/x-prd:prd/x-prd.md#1"
    b = "draft/x-prd:prd/x-prd.md#2"
    graph = store.rag.chunk_entity_relation_graph
    docs = [DocumentRef("draft/x-prd", "prd/x-prd.md")]
    a_refs_b = _payload(a, "## 1. 하나\n§2 참고", docs)
    b_refs_a = _payload(b, "## 2. 둘\n§1 참고", docs)
    a_alone = _payload(a, "## 1. 하나\n참조 없음", docs)
    await store.upsert_chapter_kg(a_refs_b, previous=None)
    await store.upsert_chapter_kg(b_refs_a, previous=None)

    await store.upsert_chapter_kg(a_alone, previous=a_refs_b.keys, keep=frozenset({(a, b)}))

    assert await graph.has_edge(a, b)


async def test_store_passes_chunk_size_and_language_from_settings(settings_env, tmp_path):
    """R2-A2 · T7: 토큰 한도(CHUNK_SIZE)와 요약 언어가 Settings 에서 LightRAG 로 전달된다."""
    from lightrag.utils import EmbeddingFunc, Tokenizer

    from specgraph.settings import load_settings

    settings_env.setenv("SPECGRAPH_WORKING_DIR", str(tmp_path / "rag-cfg"))
    settings_env.setenv("LIGHTRAG_KV_STORAGE", "JsonKVStorage")
    settings_env.setenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage")
    settings_env.setenv("LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage")
    settings_env.setenv("LIGHTRAG_DOC_STATUS_STORAGE", "JsonDocStatusStorage")
    settings = load_settings().model_copy(update={"chunk_token_size": 777})
    real = await LightRagStore.create(
        settings,
        LlmCallCounter(),
        llm_model_func=_fake_llm,
        embedding_func=EmbeddingFunc(embedding_dim=16, func=_fake_embed, model_name="fake"),
        tokenizer=Tokenizer("char", CharTokenizer()),
        rerank_model_func=None,
        workspace=_workspace(tmp_path / "rag-cfg"),
    )
    try:
        assert real.rag.chunk_token_size == 777
        assert real.rag.addon_params["language"] == "Korean"
    finally:
        await real.close()


async def test_drop_workspace_empties_every_storage(store):
    """R2-A4: 통합 테스트 정리를 어댑터 공개 메서드로 한다(내부 스토리지 직접 접근 금지)."""
    await store.insert_chapter(DOC, ["## 5. 정산 정책\nP-14.3"])
    await store.upsert_chapter_kg(_payload(OTHER, "## 2. PTN-P-04\nPTN-P-04"), previous=None)

    await store.drop_workspace()

    assert await store.rag.aget_docs_by_ids([DOC]) == {}
    assert await store.entity_names() == []


async def test_real_lightrag_shared_screen_keeps_other_chapter_file_path_after_reinsert(store):
    """R3-C1 (AC8): A · B 가 같은 화면을 참조하고 A 만 재삽입해도 file_path 에 B 가 남는다."""
    graph = store.rag.chunk_entity_relation_graph
    a_first = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면")
    a_again = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면 (개정)")
    b = _payload(PARTNER3, "## 3. 이력\nPTN-P-04 에서 이동")
    await store.upsert_chapter_kg(a_first, previous=None)
    await store.upsert_chapter_kg(b, previous=None)

    await store.delete_chapter(OTHER)
    await store.insert_chapter(OTHER, ["## 2. PTN-P-04 정산 조회\nPTN-P-04 화면 (개정)"])
    await store.upsert_chapter_kg(a_again, previous=a_first.keys)

    node = await graph.get_node("PTN-P-04")
    assert PARTNER3 in decode_sources(node["file_path"])
    assert OTHER in decode_sources(node["file_path"])
    assert _anchor_id(b.keys.anchor_content) in _sep(node["source_id"])


def _anchor_id(content):
    from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding

    return compute_mdhash_id(sanitize_text_for_encoding(content), prefix="chunk-")


async def test_real_lightrag_shared_node_restore_makes_zero_llm_calls(settings_env, tmp_path):
    """R3-C1 (AC9): 공유 노드 출처 복원(재삽입의 KG 단계)은 LLM 을 부르지 않는다."""
    calls: list[str] = []

    async def counting_llm(prompt: str, system_prompt: str | None = None, **_: object) -> str:
        calls.append(prompt)
        return ""

    from lightrag.utils import Tokenizer

    real = await _create_store(
        settings_env,
        tmp_path / "rag-shared",
        llm_model_func=counting_llm,
        tokenizer=Tokenizer("char", CharTokenizer()),
    )
    a_first = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면")
    a_again = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면 (개정)")
    b = _payload(PARTNER3, "## 3. 이력\nPTN-P-04 에서 이동")
    try:
        await real.upsert_chapter_kg(a_first, previous=None)
        await real.upsert_chapter_kg(b, previous=None)
        before = len(calls)

        await real.upsert_chapter_kg(a_again, previous=a_first.keys)

        node = await real.rag.chunk_entity_relation_graph.get_node("PTN-P-04")
        assert PARTNER3 in decode_sources(node["file_path"])
    finally:
        await real.close()
    assert len(calls) - before == 0


# --- 삭제 결과 전파 (R3-C2 · AC6) ----------------------------------------------------------


class _DeleteStubRag:
    """adelete_by_relation / adelete_by_entity 가 status 로 결과를 돌려주는 스텁."""

    def __init__(self, statuses: dict[tuple, list[str]] | None = None) -> None:
        self._statuses = statuses or {}
        self.relation_calls: list[tuple[str, str]] = []
        self.entity_calls: list[str] = []

    def _next(self, key: tuple) -> object:
        from types import SimpleNamespace

        queue = self._statuses.get(key)
        status = queue.pop(0) if queue else "success"
        return SimpleNamespace(status=status, message=f"stub {status}")

    async def adelete_by_relation(self, src, tgt):
        self.relation_calls.append((src, tgt))
        return self._next(("relation", src, tgt))

    async def adelete_by_entity(self, name):
        self.entity_calls.append(name)
        return self._next(("entity", name))


def _keys(relations, chapter="CH"):
    from specgraph.kg_model import KgKeys

    return KgKeys(chapter, tuple(relations), "anchor")


async def test_remove_relation_fail_status_raises_indexing_error():
    from specgraph.errors import IndexingError

    rag = _DeleteStubRag({("relation", "a", "b"): ["fail"]})

    with pytest.raises(IndexingError):
        await LightRagStore(rag)._remove_relations([("a", "b")])


async def test_remove_relation_not_found_is_treated_as_success():
    rag = _DeleteStubRag({("relation", "a", "b"): ["not_found"]})

    await LightRagStore(rag)._remove_relations([("a", "b"), ("c", "d")])

    assert rag.relation_calls == [("a", "b"), ("c", "d")]


async def test_remove_relation_not_allowed_status_raises_indexing_error():
    from specgraph.errors import IndexingError

    rag = _DeleteStubRag({("relation", "a", "b"): ["not_allowed"]})

    with pytest.raises(IndexingError):
        await LightRagStore(rag)._remove_relations([("a", "b")])


async def test_delete_entity_fail_status_raises_indexing_error():
    from specgraph.errors import IndexingError

    rag = _DeleteStubRag({("entity", "P-1"): ["fail"]})

    with pytest.raises(IndexingError):
        await LightRagStore(rag).delete_entities(["P-1"])


async def test_delete_entity_not_found_is_treated_as_success():
    rag = _DeleteStubRag({("entity", "P-1"): ["not_found"]})

    await LightRagStore(rag).delete_entities(["P-1", "P-2"])

    assert rag.entity_calls == ["P-1", "P-2"]


async def test_delete_chapter_kg_fail_status_raises_indexing_error():
    from specgraph.errors import IndexingError

    rag = _DeleteStubRag({("entity", "CH"): ["fail"]})
    store = LightRagStore(rag)

    async def no_detach(*_args, **_kwargs):
        return None

    store._detach = no_detach  # 이 테스트는 삭제 결과 처리만 본다

    with pytest.raises(IndexingError):
        await store.delete_chapter_kg(_keys([]))


async def test_partial_relation_delete_retry_is_idempotent():
    """첫 시도는 두 번째 관계에서 실패하고, 재시도는 이미 지운 첫 관계를 not_found 로 통과한다."""
    from specgraph.errors import IndexingError

    rag = _DeleteStubRag({("relation", "c", "d"): ["fail"], ("relation", "a", "b"): ["success"]})
    store = LightRagStore(rag)
    relations = [("a", "b"), ("c", "d")]

    with pytest.raises(IndexingError):
        await store._remove_relations(relations)
    rag._statuses[("relation", "a", "b")] = ["not_found"]
    await store._remove_relations(relations)

    assert rag.relation_calls == [("a", "b"), ("c", "d"), ("a", "b"), ("c", "d")]


async def test_real_lightrag_intermediate_anchor_chunk_leaves_no_trace_in_chunks_and_node_source_id(
    store,
):
    """R3-C3 (AC10): 삽입 실패 뒤 내용이 또 바뀌어도 중간 앵커 청크가 어디에도 남지 않는다."""
    from specgraph.kg_model import KgKeys

    graph = store.rag.chunk_entity_relation_graph
    v0 = _payload(OTHER, "## 2. PTN-P-04 정산 조회\nPTN-P-04 화면")
    v1 = _payload(OTHER, "## 2. PTN-P-04 정산 조회 (중간)\nPTN-P-04 화면")
    v2 = _payload(OTHER, "## 2. PTN-P-04 정산 조회 (최종)\nPTN-P-04 화면")
    shared = _payload(PARTNER3, "## 3. 이력\nPTN-P-04 에서 이동")
    await store.upsert_chapter_kg(shared, previous=None)
    await store.upsert_chapter_kg(v0, previous=None)
    # 시도 1: KG 는 들어갔지만 manifest 완료 기록이 실패 → PENDING 이 중간 앵커를 stale 로 기억한다.
    await store.upsert_chapter_kg(v1, previous=v0.keys)
    pending = KgKeys(
        OTHER,
        tuple(dict.fromkeys((*v0.keys.relations, *v1.keys.relations))),
        v0.keys.anchor_content,
        stale_anchors=(v1.keys.anchor_content,),
    )

    await store.upsert_chapter_kg(v2, previous=pending)

    intermediate = _anchor_id(v1.keys.anchor_content)
    assert await store.rag.text_chunks.get_by_id(intermediate) is None
    assert await store.rag.chunks_vdb.get_by_id(intermediate) is None
    node = await graph.get_node("PTN-P-04")
    assert intermediate not in _sep(node["source_id"])
    assert _anchor_id(v2.keys.anchor_content) in _sep(node["source_id"])
    assert _anchor_id(shared.keys.anchor_content) in _sep(node["source_id"])


def test_undirected_normalizes_pair_order():
    from specgraph.lightrag_store import undirected

    assert undirected(("b", "a")) == undirected(("a", "b")) == ("a", "b")


def test_undirected_is_idempotent():
    from specgraph.lightrag_store import undirected

    once = undirected(("z", "a"))

    assert undirected(once) == once
    assert undirected(reversed(once)) == once


async def test_real_lightrag_kg_upsert_keeps_shared_edge_when_keep_is_reverse_direction(store):
    """R3-T2 (AC12): keep 을 관계의 역방향 (b, a) 로 넘겨도 공유 엣지를 지우지 않는다."""
    a = "draft/x-prd:prd/x-prd.md#1"
    b = "draft/x-prd:prd/x-prd.md#2"
    graph = store.rag.chunk_entity_relation_graph
    docs = [DocumentRef("draft/x-prd", "prd/x-prd.md")]
    a_refs_b = _payload(a, "## 1. 하나\n§2 참고", docs)
    b_refs_a = _payload(b, "## 2. 둘\n§1 참고", docs)
    a_alone = _payload(a, "## 1. 하나\n참조 없음", docs)
    await store.upsert_chapter_kg(a_refs_b, previous=None)
    await store.upsert_chapter_kg(b_refs_a, previous=None)

    await store.upsert_chapter_kg(a_alone, previous=a_refs_b.keys, keep=frozenset({(b, a)}))

    assert await graph.has_edge(a, b)
