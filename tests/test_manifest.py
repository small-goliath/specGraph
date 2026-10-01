import pytest

from specgraph.kg_model import KgKeys
from specgraph.manifest import ChapterRecord, render_ddl, validate_schema


def test_render_ddl_uses_given_schema():
    ddl = render_ddl("itest_abc")

    assert "CREATE SCHEMA IF NOT EXISTS itest_abc;" in ddl
    assert "itest_abc.chapters" in ddl and "itest_abc.branch_heads" in ddl
    assert "specgraph." not in ddl


def test_default_schema_is_specgraph():
    assert "CREATE SCHEMA IF NOT EXISTS specgraph;" in render_ddl()


@pytest.mark.parametrize("bad", ["", "a-b", "x; DROP TABLE y", "1abc", "A" * 64])
def test_invalid_schema_rejected(bad):
    with pytest.raises(ValueError):
        validate_schema(bad)


def test_record_document_and_with_commit():
    rec = ChapterRecord(
        doc_id="draft/a:prd/a.md#1",
        branch="draft/a",
        path="prd/a.md",
        section="1",
        title="1. A",
        content="c",
        content_hash="h",
        commit_sha="s1",
        content_commit_sha="s1",
        kg_keys=KgKeys("draft/a:prd/a.md#1", (), "anchor"),
    )

    moved = rec.with_commit("s2")

    assert rec.document == "draft/a:prd/a.md"
    assert moved.commit_sha == "s2" and moved.content_commit_sha == "s1"


class _RecordingPool:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def close(self):
        self.closed = True


async def test_drop_schema_drops_own_schema():
    """R2-A4: 통합 테스트 정리를 공개 메서드로(private _pool 직접 접근 금지)."""
    from specgraph.manifest import PostgresManifest

    pool = _RecordingPool()
    manifest = PostgresManifest(pool, schema="itest_abc")

    await manifest.drop_schema()

    assert pool.executed == ["DROP SCHEMA IF EXISTS itest_abc CASCADE"]
