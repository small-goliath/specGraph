import pytest

from fakes import InMemoryManifest
from specgraph.errors import NotFoundError
from specgraph.indexer.chapters import split_chapters
from specgraph.indexer.extract import extract_chapter
from specgraph.manifest import ChapterRecord
from specgraph.mcp_server.lookup import LookupService

ADMIN_BRANCH = "draft/settlr-admin-prd"
PARTNER_BRANCH = "draft/settlr-partner-prd"


def _load(manifest, fixture_docs, branch, path, name, sha):
    text = (fixture_docs / name).read_text(encoding="utf-8")
    for ch in split_chapters(text):
        refs = extract_chapter(ch.text)
        doc_id = f"{branch}:{path}#{ch.section}"
        manifest.records[doc_id] = ChapterRecord(
            doc_id=doc_id,
            branch=branch,
            path=path,
            section=ch.section,
            title=ch.title,
            content=ch.text,
            content_hash=ch.content_hash,
            commit_sha=sha,
            content_commit_sha=sha,
            screens=refs.screen_ranks,
            policies=refs.policy_ranks,
        )


@pytest.fixture
def lookup(fixture_docs):
    m = InMemoryManifest()
    _load(m, fixture_docs, ADMIN_BRANCH, "prd/settlr-admin-prd.md", "settlr-admin-prd.md", "sha-a")
    _load(
        m,
        fixture_docs,
        PARTNER_BRANCH,
        "prd/settlr-partner-prd.md",
        "settlr-partner-prd.md",
        "sha-p",
    )
    m.heads = {ADMIN_BRANCH: "sha-a", PARTNER_BRANCH: "sha-p"}
    return LookupService(m)


async def test_find_screen_returns_defining_chapter_text_and_doc_id(lookup):
    result = await lookup.find_screen("ADM-03")

    assert result["doc_id"] == f"{ADMIN_BRANCH}:prd/settlr-admin-prd.md#3"
    assert result["commit_sha"] == "sha-a"
    assert result["content"].startswith("## 3. ADM-03 정산 내역")
    assert f"{ADMIN_BRANCH}:prd/settlr-admin-prd.md#2" in result["mentioned_in"]


async def test_find_screen_is_case_and_space_insensitive(lookup):
    assert (await lookup.find_screen(" adm-03 "))["doc_id"].endswith("#3")


async def test_find_screen_prefers_table_definition_over_mention(lookup):
    result = await lookup.find_screen("AUTH-01")

    assert result["doc_id"] == f"{ADMIN_BRANCH}:prd/settlr-admin-prd.md#2"


async def test_find_policy_returns_defining_chapter(lookup):
    result = await lookup.find_policy("P-14.3")

    assert result["doc_id"] == f"{ADMIN_BRANCH}:prd/settlr-admin-prd.md#5"
    assert "| P-14.3 | 정산 주기 D+2 |" in result["content"]
    assert result["commit_sha"] == "sha-a"


@pytest.mark.parametrize(
    "doc",
    [
        f"{ADMIN_BRANCH}:prd/settlr-admin-prd.md",
        ADMIN_BRANCH,
        "settlr-admin-prd",
        "admin-prd",
    ],
)
async def test_get_chapter_accepts_doc_id_prefix_or_short_name(lookup, doc):
    result = await lookup.get_chapter(doc, "§5.2")

    assert result["doc_id"] == f"{ADMIN_BRANCH}:prd/settlr-admin-prd.md#5"
    assert result["commit_sha"] == "sha-a"
    assert result["content"].startswith("## 5. 정산 정책")


async def test_get_chapter_accepts_full_doc_id(lookup):
    result = await lookup.get_chapter(f"{PARTNER_BRANCH}:prd/settlr-partner-prd.md#2", "")

    assert result["doc_id"].endswith("#2")


async def test_get_chapter_ambiguous_doc_raises_not_found(lookup):
    with pytest.raises(NotFoundError) as exc:
        await lookup.get_chapter("prd", "1")

    assert "settlr-admin-prd" in str(exc.value) and "settlr-partner-prd" in str(exc.value)


async def test_list_docs_groups_by_branch_with_sha(lookup):
    result = await lookup.list_docs()

    branches = {b["branch"]: b for b in result["branches"]}
    assert set(branches) == {ADMIN_BRANCH, PARTNER_BRANCH}
    admin = branches[ADMIN_BRANCH]
    assert admin["commit_sha"] == "sha-a"
    doc = admin["documents"][0]
    assert doc["path"] == "prd/settlr-admin-prd.md"
    assert [c["section"] for c in doc["chapters"]] == ["preamble", "0", "1", "2", "3", "5"]
    assert all(c["doc_id"] and c["commit_sha"] == "sha-a" for c in doc["chapters"])


async def test_list_docs_branch_commit_sha_comes_from_branch_head():
    """C13: 브랜치 commit_sha 는 첫 챕터가 아니라 manifest 의 브랜치 HEAD 다."""
    old = ChapterRecord(
        doc_id=f"{ADMIN_BRANCH}:prd/a.md#1",
        branch=ADMIN_BRANCH,
        path="prd/a.md",
        section="1",
        title="1. 하나",
        content="## 1. 하나",
        content_hash="h",
        commit_sha="sha-old",
        content_commit_sha="sha-old",
    )
    manifest = InMemoryManifest(records={old.doc_id: old}, heads={ADMIN_BRANCH: "sha-head"})

    result = await LookupService(manifest).list_docs()

    assert result["branches"][0]["commit_sha"] == "sha-head"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.find_screen("ZZZ-99"),
        lambda s: s.find_policy("Q-1"),
        lambda s: s.get_chapter("settlr-admin-prd", "42"),
        lambda s: s.get_chapter("unknown-doc", "1"),
    ],
)
async def test_unknown_id_raises_not_found(lookup, call):
    with pytest.raises(NotFoundError):
        await call(lookup)
