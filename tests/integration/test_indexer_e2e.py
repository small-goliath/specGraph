"""AC6 · AC8 · AC9 · AC13 · AC14 — 실제 PostgreSQL · LightRAG · Ollama 로 인덱싱.

로컬 bare repo 에 합성 문서 두 브랜치를 올리고 IndexerService 를 실제 어댑터로 돌린다.
"""

import logging
import re

import pytest

from specgraph.indexer.chapters import split_chapters
from specgraph.indexer.git_source import GitSource
from specgraph.indexer.glossary import parse_glossary
from specgraph.indexer.service import IndexerService
from specgraph.lightrag_store import decode_sources

pytestmark = pytest.mark.integration

ADMIN = ("draft/settlr-admin-prd", "settlr/prd/settlr-admin-prd.md", "settlr-admin-prd.md")
PARTNER = ("draft/settlr-partner-prd", "settlr/prd/settlr-partner-prd.md", "settlr-partner-prd.md")


@pytest.fixture
def remote(git_remote, fixture_docs):
    r = git_remote()
    for branch, path, name in (ADMIN, PARTNER):
        r.commit(branch, {path: (fixture_docs / name).read_text(encoding="utf-8")})
    return r


@pytest.fixture
def service(remote, live_store, pg_manifest, counter, live_settings, tmp_path):
    return IndexerService(
        git=GitSource(repo_url=remote.url, cache_dir=tmp_path / "mirror", token=None),
        rag=live_store,
        manifest=pg_manifest,
        counter=counter,
        include_dirs=live_settings.include_dirs,
        max_block_chars=live_settings.max_block_chars,
    )


async def test_index_reindex_and_remove_branch(
    service, remote, live_store, pg_manifest, fixture_docs, caplog
):
    # 별칭 엔티티를 미리 넣어 둔다 — LLM 이 별칭을 추출하지 않아도 병합 경로가 반드시 실행된다(T5).
    await live_store.rag.acreate_entity(
        "Settlement", {"description": "영문 별칭으로 추출된 정산", "entity_type": "concept"}
    )

    # --- 최초 인덱싱: doc_id · 커밋 SHA (AC8) -------------------------------------
    first = await service.poll_once()
    assert first.failed == {}
    admin5 = f"{ADMIN[0]}:{ADMIN[1]}#5"
    record = await pg_manifest.get_chapter(admin5)
    assert record.commit_sha == remote.shas[ADMIN[0]]
    statuses = await live_store.rag.aget_docs_by_ids([admin5])
    assert admin5 in statuses

    # --- custom KG: LLM 없이 삽입된 화면 · 정책 노드와 문서 간 엣지 (AC13) ----------------
    graph = live_store.rag.chunk_entity_relation_graph
    screen = await graph.get_node("ADM-03")
    assert screen is not None
    assert f"{ADMIN[0]}:{ADMIN[1]}#3" in decode_sources(screen["file_path"])
    assert await graph.has_edge(f"{PARTNER[0]}:{PARTNER[1]}#2", admin5)

    # --- 용어 사전 병합 후 별칭 엔티티가 남지 않는다 (AC14) -----------------------------
    glossary = parse_glossary(
        split_chapters((fixture_docs / ADMIN[2]).read_text(encoding="utf-8"))
    ).merge(parse_glossary(split_chapters((fixture_docs / PARTNER[2]).read_text(encoding="utf-8"))))
    names = set(await live_store.entity_names())
    assert "Settlement" not in names and "정산" in names  # 미리 넣은 별칭이 표준어로 합쳐졌다
    assert glossary.merge_plan(names) == []

    # --- 챕터 1개 수정 → 그 챕터만 재삽입, 나머지 LLM 0회 (AC9) ---------------------------
    text = (fixture_docs / ADMIN[2]).read_text(encoding="utf-8")
    remote.commit(ADMIN[0], {ADMIN[1]: text.replace("D+2 이며", "D+3 이며")})
    caplog.set_level(logging.INFO)
    await service.poll_once()
    lines = [r.getMessage() for r in caplog.records if "event=chapter_sync" in r.getMessage()]
    skips = [ln for ln in lines if "action=skip" in ln]
    changed = [ln for ln in lines if "action=reinsert" in ln]
    assert skips and all("llm_calls=0" in ln for ln in skips)
    assert len(changed) == 1 and admin5 in changed[0]
    assert int(re.search(r"llm_calls=(\d+)", changed[0]).group(1)) > 0

    # --- 브랜치 삭제 → 다음 주기에 인덱스에서 제거 (AC6) ----------------------------------
    remote.delete_branch(PARTNER[0])
    result = await service.poll_once()
    assert result.removed == [PARTNER[0]]
    partner2 = f"{PARTNER[0]}:{PARTNER[1]}#2"
    assert await live_store.rag.aget_docs_by_ids([partner2]) == {}
    assert not await graph.has_node(partner2)
    assert await pg_manifest.chapters_for_branch(PARTNER[0]) == {}
