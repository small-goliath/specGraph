"""AC8 — manifest(PostgreSQL) 레코드에 doc_id · 커밋 SHA 가 남는다."""

import pytest

from specgraph.kg_model import KgKeys
from specgraph.manifest import ChapterRecord

pytestmark = pytest.mark.integration

DOC = "draft/a-prd:prd/a-prd.md#5"


def _record(sha="s1"):
    return ChapterRecord(
        doc_id=DOC,
        branch="draft/a-prd",
        path="prd/a-prd.md",
        section="5",
        title="5. 정산",
        content="## 5. 정산\nADM-03 P-14.3",
        content_hash="h1",
        commit_sha=sha,
        content_commit_sha=sha,
        screens={"ADM-03": 0},
        policies={"P-14.3": 2},
        kg_keys=KgKeys(DOC, ((DOC, "ADM-03"),), "[anchor]"),
    )


async def test_chapter_roundtrip_with_commit_shas(pg_manifest):
    await pg_manifest.upsert_chapter(_record())
    await pg_manifest.touch_chapters([DOC], "s2")

    record = await pg_manifest.get_chapter(DOC)

    assert record.commit_sha == "s2"
    assert record.content_commit_sha == "s1"
    assert record.screens == {"ADM-03": 0}
    assert record.kg_keys == _record().kg_keys
    assert [r.doc_id for r in await pg_manifest.chapters_with_screen("ADM-03")] == [DOC]
    assert [r.doc_id for r in await pg_manifest.chapters_with_policy("P-14.3")] == [DOC]
    assert await pg_manifest.referenced_names(["ADM-03", "X-01", "draft/a-prd:prd/a-prd.md"]) == {
        "ADM-03",
        "draft/a-prd:prd/a-prd.md",
    }


async def test_branch_heads_and_delete(pg_manifest):
    await pg_manifest.set_branch_head("draft/a-prd", "s1")
    await pg_manifest.set_branch_head("draft/a-prd", "s2")
    await pg_manifest.upsert_chapter(_record())

    assert await pg_manifest.branch_heads() == {"draft/a-prd": "s2"}
    assert list(await pg_manifest.chapters_for_branch("draft/a-prd")) == [DOC]
    assert await pg_manifest.chapter_branches() == {"draft/a-prd"}

    await pg_manifest.delete_chapter(DOC)
    await pg_manifest.delete_branch_head("draft/a-prd")

    assert await pg_manifest.all_chapters() == []
    assert await pg_manifest.branch_heads() == {}


async def test_advisory_lock_blocks_second_indexer(pg_manifest, live_settings, unique):
    from specgraph.manifest import PostgresManifest

    other = await PostgresManifest.create(live_settings.postgres_dsn(), schema=unique)
    try:
        assert await pg_manifest.try_acquire_indexer_lock()
        assert not await other.try_acquire_indexer_lock()
    finally:
        await other.close()
