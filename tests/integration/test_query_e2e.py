"""AC15 · AC16 · AC17 · AC19 — 실제 스택에서 질의 · 조회."""

import pytest

from specgraph.indexer.git_source import GitSource
from specgraph.indexer.service import IndexerService
from specgraph.mcp_server.lookup import LookupService
from specgraph.mcp_server.query_service import QueryService

pytestmark = pytest.mark.integration

ADMIN = ("draft/settlr-admin-prd", "settlr/prd/settlr-admin-prd.md", "settlr-admin-prd.md")
PARTNER = ("draft/settlr-partner-prd", "settlr/prd/settlr-partner-prd.md", "settlr-partner-prd.md")


async def test_cross_branch_and_branch_scoped_query(
    git_remote, fixture_docs, live_store, pg_manifest, counter, live_settings, tmp_path
):
    remote = git_remote()
    for branch, path, name in (ADMIN, PARTNER):
        remote.commit(branch, {path: (fixture_docs / name).read_text(encoding="utf-8")})
    indexer = IndexerService(
        git=GitSource(repo_url=remote.url, cache_dir=tmp_path / "mirror", token=None),
        rag=live_store,
        manifest=pg_manifest,
        counter=counter,
    )
    assert (await indexer.poll_once()).failed == {}
    queries = QueryService(live_store, pg_manifest)  # PPS-350: 질의 서비스는 LLM 이 없다
    calls_before_query = counter.total

    merged = await queries.query(
        "파트너 정산 조회 화면의 정산 주기 근거가 되는 정책은?",
        "mix",
        None,
        ll_keywords=["정산 주기", "정책"],
    )
    prefixes = {s.doc_id.split(":", 1)[0] for s in merged.sources}
    assert {ADMIN[0], PARTNER[0]}.issubset(prefixes)  # AC15
    assert all(s.commit_sha for s in merged.sources)  # AC17
    assert not merged.no_evidence and merged.chunks  # PPS-350 AC14

    scoped = await queries.query("정산 주기 근거는?", "mix", PARTNER[0], ll_keywords=["정산 주기"])
    assert scoped.sources
    assert all(s.doc_id.startswith(f"{PARTNER[0]}:") for s in scoped.sources)  # AC16
    assert counter.total == calls_before_query  # PPS-350 AC12: 질의 경로 LLM 0 건

    screen = await LookupService(pg_manifest).find_screen("ADM-03")
    assert screen["doc_id"] == f"{ADMIN[0]}:{ADMIN[1]}#3"  # AC19
    assert screen["content"].startswith("## 3. ADM-03")
