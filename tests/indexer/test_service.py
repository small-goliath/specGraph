import logging
import re

import pytest

from fakes import FakeLightRAG, InMemoryManifest
from specgraph.indexer.git_source import GitSource
from specgraph.indexer.service import IndexerService
from specgraph.llm import LlmCallCounter

PROJECT = "settlr"
ADMIN_BRANCH = "draft/settlr-admin-prd"
PARTNER_BRANCH = "draft/settlr-partner-prd"
ADMIN_PATH = f"{PROJECT}/prd/settlr-admin-prd.md"
PARTNER_PATH = f"{PROJECT}/prd/settlr-partner-prd.md"


@pytest.fixture
def docs(fixture_docs):
    return {
        "admin": (fixture_docs / "settlr-admin-prd.md").read_text(encoding="utf-8"),
        "partner": (fixture_docs / "settlr-partner-prd.md").read_text(encoding="utf-8"),
    }


@pytest.fixture
def env(git_remote, tmp_path, docs):
    remote = git_remote()
    remote.commit(
        ADMIN_BRANCH,
        {
            ADMIN_PATH: docs["admin"],
            f"{PROJECT}/README.md": "# readme",
            f"{PROJECT}/prd/a.docx": "bin",
        },
    )
    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"], f"{PROJECT}/CLAUDE.md": "x"})
    remote.commit("main", {"README.md": "main"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    manifest = InMemoryManifest()
    git = GitSource(repo_url=remote.url, cache_dir=tmp_path / "mirror", token=None)
    service = IndexerService(
        git=git,
        rag=rag,
        manifest=manifest,
        counter=counter,
        include_dirs=("prd", "ui-ux-spec", "tech-spec", "qa"),
        max_block_chars=1000,
    )
    return remote, service, rag, manifest, git


def _events(caplog, event):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"event={event} ")]


async def test_first_poll_inserts_all_chapters_with_sha(env):
    remote, service, rag, manifest, _ = env

    result = await service.poll_once()

    admin_sha = remote.shas[ADMIN_BRANCH]
    assert sorted(result.indexed) == [ADMIN_BRANCH, PARTNER_BRANCH]
    assert rag.doc_ids(f"{ADMIN_BRANCH}:") == [
        f"{ADMIN_BRANCH}:{ADMIN_PATH}#{s}" for s in ["0", "1", "2", "3", "5", "preamble"]
    ]
    record = manifest.records[f"{ADMIN_BRANCH}:{ADMIN_PATH}#5"]
    assert record.commit_sha == admin_sha
    assert record.content_commit_sha == admin_sha
    assert record.section == "5" and record.path == ADMIN_PATH and record.branch == ADMIN_BRANCH
    assert manifest.heads == {ADMIN_BRANCH: admin_sha, PARTNER_BRANCH: remote.shas[PARTNER_BRANCH]}


async def test_non_target_files_are_not_indexed(env):
    _, service, rag, _, _ = env

    await service.poll_once()

    assert not any("README" in d or "CLAUDE" in d or ".docx" in d for d in rag.chapters)


async def test_blocks_passed_to_lightrag_keep_tables_whole(env):
    _, service, rag, _, _ = env

    await service.poll_once()

    blocks = rag.chapters[f"{ADMIN_BRANCH}:{ADMIN_PATH}#2"]
    table = next(b for b in blocks if "| ADM-03 |" in b)
    assert "| 화면 ID | 화면명 | 비고 |" in table and "| AUTH-01 |" in table


async def test_custom_kg_inserted_for_each_chapter_with_cross_doc_edge(env):
    _, service, rag, _, _ = env

    await service.poll_once()

    partner2 = rag.kg[f"{PARTNER_BRANCH}:{PARTNER_PATH}#2"]
    edges = {(r["tgt_id"], r["keywords"]) for r in partner2.custom_kg["relationships"]}
    assert (f"{ADMIN_BRANCH}:{ADMIN_PATH}#5", "REFERS_TO") in edges
    assert ("PTN-P-04", "DEFINES") in edges


async def test_manifest_records_screen_definition_ranks(env):
    _, service, _, manifest, _ = env

    await service.poll_once()

    ch3 = manifest.records[f"{ADMIN_BRANCH}:{ADMIN_PATH}#3"]
    ch2 = manifest.records[f"{ADMIN_BRANCH}:{ADMIN_PATH}#2"]
    assert ch3.screens["ADM-03"] == 0
    assert ch2.screens["ADM-03"] == 1
    assert "## 3. ADM-03 정산 내역" in ch3.content


async def test_unchanged_head_skips_fetch_and_lightrag(env):
    _, service, rag, _, git = env
    await service.poll_once()
    rag.calls.clear()
    git.command_log.clear()

    result = await service.poll_once()

    assert result.indexed == [] and result.removed == []
    assert sorted(result.unchanged) == [ADMIN_BRANCH, PARTNER_BRANCH]
    assert rag.calls == []
    assert [argv for argv in git.command_log if "fetch" in argv] == []


async def test_single_chapter_change_reinserts_only_that_chapter(env, docs):
    remote, service, rag, manifest, _ = env
    await service.poll_once()
    rag.calls.clear()

    new_sha = remote.commit(
        ADMIN_BRANCH, {ADMIN_PATH: docs["admin"].replace("D+2 이며", "D+3 이며")}
    )
    await service.poll_once()

    changed = f"{ADMIN_BRANCH}:{ADMIN_PATH}#5"
    touched = {c[1] for c in rag.calls if c[0] in ("insert", "delete", "upsert_kg", "delete_kg")}
    assert touched == {changed}
    assert ("delete", changed) in rag.calls and ("insert", changed) in rag.calls
    assert manifest.records[changed].content_commit_sha == new_sha
    unchanged = manifest.records[f"{ADMIN_BRANCH}:{ADMIN_PATH}#3"]
    assert unchanged.commit_sha == new_sha
    assert unchanged.content_commit_sha != new_sha


async def test_unchanged_chapters_logged_with_zero_llm_calls(env, docs, caplog):
    remote, service, _, _, _ = env
    await service.poll_once()
    remote.commit(ADMIN_BRANCH, {ADMIN_PATH: docs["admin"].replace("D+2 이며", "D+3 이며")})
    caplog.set_level(logging.INFO)

    await service.poll_once()

    lines = _events(caplog, "chapter_sync")
    skip = [ln for ln in lines if "action=skip" in ln]
    assert len(skip) == 5
    assert all("llm_calls=0" in ln for ln in skip)
    changed = [ln for ln in lines if "action=reinsert" in ln]
    assert len(changed) == 1 and "#5 " in changed[0]
    assert int(re.search(r"llm_calls=(\d+)", changed[0]).group(1)) > 0
    done = _events(caplog, "sync_done")
    assert len(done) == 1 and f"branch={ADMIN_BRANCH}" in done[0]
    assert "elapsed_s=" in done[0] and "llm_calls_total=" in done[0]
    assert "reinserted=1" in done[0] and "skipped=5" in done[0]


async def test_new_branch_indexed_next_poll(env):
    remote, service, rag, _, _ = env
    await service.poll_once()

    remote.commit(
        "draft/settlr-new-prd", {f"{PROJECT}/prd/settlr-new-prd.md": "## 1. 새 문서\n본문"}
    )
    result = await service.poll_once()

    assert result.indexed == ["draft/settlr-new-prd"]
    assert rag.doc_ids("draft/settlr-new-prd:") == [
        f"draft/settlr-new-prd:{PROJECT}/prd/settlr-new-prd.md#1"
    ]


async def test_deleted_branch_removed_next_poll(env):
    remote, service, rag, manifest, _ = env
    await service.poll_once()

    remote.delete_branch(PARTNER_BRANCH)
    result = await service.poll_once()

    assert result.removed == [PARTNER_BRANCH]
    assert rag.doc_ids(f"{PARTNER_BRANCH}:") == []
    assert not any(d.startswith(f"{PARTNER_BRANCH}:") for d in rag.kg)
    assert not any(r.branch == PARTNER_BRANCH for r in manifest.records.values())
    assert PARTNER_BRANCH not in manifest.heads
    assert rag.doc_ids(f"{ADMIN_BRANCH}:")


async def test_orphan_screen_entities_removed_with_branch(env):
    remote, service, rag, _, _ = env
    await service.poll_once()

    remote.delete_branch(PARTNER_BRANCH)
    await service.poll_once()

    deleted = [c[1] for c in rag.calls if c[0] == "delete_entities"]
    flat = {name for names in deleted for name in names}
    assert "PTN-P-04" in flat
    assert f"{PARTNER_BRANCH}:{PARTNER_PATH}" in flat
    assert "AUTH-01" not in flat  # admin 문서가 여전히 참조


async def test_removed_file_deletes_its_chapters(env):
    remote, service, rag, _, _ = env
    await service.poll_once()

    remote.commit(
        PARTNER_BRANCH, {PARTNER_PATH: None, f"{PROJECT}/prd/other.md": "## 1. 다른\n본문"}
    )
    await service.poll_once()

    assert rag.doc_ids(f"{PARTNER_BRANCH}:") == [f"{PARTNER_BRANCH}:{PROJECT}/prd/other.md#1"]


async def test_glossary_aliases_merged_after_insert(env):
    _, service, rag, _, _ = env
    rag.extracted_entities.update({"정산", "Settlement", "세틀먼트", "제휴사", "무관"})

    await service.poll_once()

    merges = {target: sorted(sources) for sources, target in rag.merges}
    assert merges == {"정산": ["Settlement", "세틀먼트"], "파트너": ["제휴사"]}


async def test_glossary_not_applied_when_nothing_changed(env):
    _, service, rag, _, _ = env
    await service.poll_once()
    rag.extracted_entities.update({"Settlement"})

    await service.poll_once()

    assert rag.merges == []


async def test_branch_failure_isolated_and_retried(env, caplog):
    remote, service, rag, manifest, _ = env
    rag.fail_on.add(PARTNER_BRANCH)
    caplog.set_level(logging.INFO)

    first = await service.poll_once()

    assert list(first.failed) == [PARTNER_BRANCH]
    assert first.indexed == [ADMIN_BRANCH]
    assert PARTNER_BRANCH not in manifest.heads
    assert _events(caplog, "branch_failed")

    rag.fail_on.clear()
    second = await service.poll_once()

    assert second.indexed == [PARTNER_BRANCH]
    assert manifest.heads[PARTNER_BRANCH] == remote.shas[PARTNER_BRANCH]


def _service(remote, tmp_path, rag, manifest, counter):
    git = GitSource(repo_url=remote.url, cache_dir=tmp_path / "mirror2", token=None)
    return IndexerService(
        git=git,
        rag=rag,
        manifest=manifest,
        counter=counter,
        include_dirs=("prd",),
        max_block_chars=1000,
    )


async def test_identical_chapter_bodies_in_two_documents_are_both_indexed(git_remote, tmp_path):
    remote = git_remote()
    template = "## 9. 변경 이력\n\n| 버전 | 일자 |\n|---|---|\n| 0.1 | 2026-09-01 |\n"
    remote.commit("draft/settlr-a-prd", {f"{PROJECT}/prd/settlr-a-prd.md": template})
    remote.commit("draft/settlr-b-prd", {f"{PROJECT}/prd/settlr-b-prd.md": template})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    service = _service(remote, tmp_path, rag, InMemoryManifest(), counter)

    result = await service.poll_once()

    assert result.failed == {}
    assert rag.doc_ids() == [
        f"draft/settlr-a-prd:{PROJECT}/prd/settlr-a-prd.md#9",
        f"draft/settlr-b-prd:{PROJECT}/prd/settlr-b-prd.md#9",
    ]


async def test_failed_first_insert_is_retried_and_indexed_next_poll(env):
    remote, service, rag, manifest, _ = env
    target = f"{PARTNER_BRANCH}:{PARTNER_PATH}#2"
    rag.fail_once.add(target)

    first = await service.poll_once()
    assert list(first.failed) == [PARTNER_BRANCH]
    assert manifest.records[target].content_hash == ""  # 시작 기록(PENDING)만 남는다

    second = await service.poll_once()

    assert second.indexed == [PARTNER_BRANCH] and second.failed == {}
    assert rag.statuses[target] == "processed"
    assert any("PTN-P-04" in b for b in rag.chapters[target])
    assert manifest.heads[PARTNER_BRANCH] == remote.shas[PARTNER_BRANCH]


async def test_insert_without_manifest_record_is_replaced_with_new_content(env, docs):
    remote, service, rag, manifest, _ = env
    target = f"{PARTNER_BRANCH}:{PARTNER_PATH}#2"
    manifest.fail_upsert_once.add(target)
    first = await service.poll_once()
    assert list(first.failed) == [PARTNER_BRANCH]

    remote.commit(
        PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("정산 내역을", "정산 이력을")}
    )
    second = await service.poll_once()

    assert second.failed == {}
    assert any("정산 이력을" in b for b in rag.chapters[target])
    assert manifest.records[target].content_hash
    assert "정산 이력을" in manifest.records[target].content


async def test_partially_indexed_new_branch_is_removed_after_remote_delete(env):
    remote, service, rag, manifest, _ = env
    rag.fail_on.add(f"{PARTNER_BRANCH}:{PARTNER_PATH}#2")
    first = await service.poll_once()
    assert list(first.failed) == [PARTNER_BRANCH]
    assert rag.doc_ids(f"{PARTNER_BRANCH}:")  # #0, #1 은 이미 들어갔다
    assert PARTNER_BRANCH not in manifest.heads

    remote.delete_branch(PARTNER_BRANCH)
    rag.fail_on.clear()
    second = await service.poll_once()

    assert second.removed == [PARTNER_BRANCH]
    assert rag.doc_ids(f"{PARTNER_BRANCH}:") == []
    assert not any(r.branch == PARTNER_BRANCH for r in manifest.records.values())


async def test_cross_doc_ref_resolved_even_when_referring_branch_is_synced_first(
    git_remote, tmp_path, docs
):
    remote = git_remote()
    referring, referred = "draft/aa-settlr-partner-prd", "draft/zz-settlr-admin-prd"
    remote.commit(referring, {PARTNER_PATH: docs["partner"]})
    remote.commit(referred, {ADMIN_PATH: docs["admin"]})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    manifest = InMemoryManifest()
    service = _service(remote, tmp_path, rag, manifest, counter)

    await service.poll_once()

    source = f"{referring}:{PARTNER_PATH}#2"
    target = f"{referred}:{ADMIN_PATH}#5"
    edges = {(r["tgt_id"], r["keywords"]) for r in rag.kg[source].custom_kg["relationships"]}
    assert (target, "REFERS_TO") in edges
    assert (source, target) in manifest.records[source].kg_keys.relations
    assert counter.total == sum(counter.count_for(d) for d in manifest.records)


def _field(line: str, key: str) -> str:
    return re.search(rf"(?:^| ){key}=(\S+)", line).group(1)


async def test_sync_llm_total_equals_sum_of_chapter_calls_with_none_unattributed(env, docs, caplog):
    remote, service, rag, _, _ = env
    rag.llm_calls_per_merge = 3
    rag.llm_calls_per_entity_delete = 1
    rag.extracted_entities.update({"정산", "Settlement", "세틀먼트"})
    caplog.set_level(logging.INFO)

    for change in (None, "PTN-P-05"):
        if change:
            remote.commit(
                PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("PTN-P-04", change)}
            )
        caplog.clear()
        await service.poll_once()

        chapter_lines = _events(caplog, "chapter_sync")
        done = _events(caplog, "sync_done")
        assert done
        for line in done:
            branch = _field(line, "branch")
            per_chapter = sum(
                int(_field(ln, "llm_calls"))
                for ln in chapter_lines
                if _field(ln, "doc_id").startswith(f"{branch}:")
            )
            assert int(_field(line, "llm_calls_total")) == per_chapter

    deleted = {n for c in rag.calls if c[0] == "delete_entities" for n in c[1]}
    assert "PTN-P-04" in deleted
    assert rag.merges
    assert service.counter.unattributed == 0


async def test_non_utf8_markdown_is_skipped_with_warning_and_branch_indexed(
    git_remote, tmp_path, caplog
):
    remote = git_remote()
    remote.commit(
        "draft/settlr-a-prd",
        {
            f"{PROJECT}/prd/good.md": "## 1. 정상\n본문",
            f"{PROJECT}/prd/bad.md": "## 1. 깨짐\n정산".encode("cp949"),
        },
    )
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    manifest = InMemoryManifest()
    service = _service(remote, tmp_path, rag, manifest, counter)
    caplog.set_level(logging.INFO)

    result = await service.poll_once()

    assert result.failed == {} and result.indexed == ["draft/settlr-a-prd"]
    assert rag.doc_ids() == [f"draft/settlr-a-prd:{PROJECT}/prd/good.md#1"]
    skipped = _events(caplog, "file_skipped")
    assert len(skipped) == 1 and f"path={PROJECT}/prd/bad.md" in skipped[0]


PARTNER2 = f"{PARTNER_BRANCH}:{PARTNER_PATH}#2"
ADMIN5 = f"{ADMIN_BRANCH}:{ADMIN_PATH}#5"


async def test_glossary_merge_llm_calls_attributed_to_glossary_key_not_a_chapter(env, caplog):
    """R2-C3: 용어 병합 LLM 호출을 특정 용어 챕터에 귀속하지 않는다(AC9 증거를 흐리지 않음)."""
    from specgraph.indexer.service import GLOSSARY_MERGE_OWNER

    _, service, rag, manifest, _ = env
    rag.llm_calls_per_merge = 3
    rag.extracted_entities.update({"정산", "Settlement", "세틀먼트", "제휴사"})
    caplog.set_level(logging.INFO)

    await service.poll_once()

    glossary_chapters = [d for d in manifest.records if d.endswith("#0")]
    assert glossary_chapters
    for doc_id in glossary_chapters:
        assert service.counter.count_for(doc_id) == rag.llm_calls_per_insert
    assert service.counter.count_for(GLOSSARY_MERGE_OWNER) == 3 * len(rag.merges)
    assert service.counter.unattributed == 0
    merge_lines = _events(caplog, "glossary_merge")
    assert merge_lines and all("llm_calls=3" in ln for ln in merge_lines)


async def test_chapter_sync_logged_even_when_later_branch_step_fails(env, docs, caplog):
    """R2-C4: 브랜치 뒷단계가 실패해도 이미 처리한 챕터의 chapter_sync(LLM 횟수)는 남는다."""
    remote, service, rag, _, _ = env
    await service.poll_once()
    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("PTN-P-04", "PTN-P-05")})
    rag.fail_entity_delete_once = True
    caplog.set_level(logging.INFO)

    result = await service.poll_once()

    assert list(result.failed) == [PARTNER_BRANCH]
    lines = [ln for ln in _events(caplog, "chapter_sync") if f"doc_id={PARTNER2} " in ln]
    assert len(lines) == 1 and "action=reinsert" in lines[0]
    assert int(_field(lines[0], "llm_calls")) > 0


async def test_chapter_sync_delete_logged_even_when_branch_removal_fails(env, caplog):
    remote, service, rag, _, _ = env
    await service.poll_once()
    remote.delete_branch(PARTNER_BRANCH)
    rag.fail_entity_delete_once = True
    caplog.set_level(logging.INFO)

    result = await service.poll_once()

    assert list(result.failed) == [PARTNER_BRANCH]
    deleted = [ln for ln in _events(caplog, "chapter_sync") if "action=delete" in ln]
    assert {_field(ln, "doc_id") for ln in deleted} >= {PARTNER2}


async def test_kg_relations_of_attempt_with_failed_manifest_write_removed_on_retry(env, docs):
    """R2-C5 · R2-T4: manifest 기록이 실패한 시도의 custom KG 관계가 재시도 때 지워진다."""
    remote, service, rag, manifest, _ = env
    manifest.fail_commit_upsert_once.add(PARTNER2)
    first = await service.poll_once()
    assert list(first.failed) == [PARTNER_BRANCH]
    assert rag.has_edge(PARTNER2, "PTN-P-04")

    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("PTN-P-04", "PTN-P-05")})
    second = await service.poll_once()

    assert second.failed == {}
    assert rag.has_edge(PARTNER2, "PTN-P-05")
    assert not rag.has_edge(PARTNER2, "PTN-P-04")


async def test_failed_lightrag_doc_of_never_completed_branch_removed_after_remote_delete(
    git_remote, tmp_path
):
    """R2-C5: 한 번도 끝까지 동기화되지 못한 브랜치의 LightRAG 문서(FAILED)도 삭제 때 정리된다."""
    remote = git_remote()
    doc = f"draft/settlr-x-prd:{PROJECT}/prd/settlr-x-prd.md#1"
    remote.commit("draft/settlr-x-prd", {f"{PROJECT}/prd/settlr-x-prd.md": "## 1. 하나\n본문"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter, fail_on={doc})
    service = _service(remote, tmp_path, rag, InMemoryManifest(), counter)
    await service.poll_once()
    assert rag.statuses.get(doc) == "failed"

    remote.delete_branch("draft/settlr-x-prd")
    result = await service.poll_once()

    assert result.removed == ["draft/settlr-x-prd"]
    assert doc not in rag.statuses


def _three_branch_env(git_remote, tmp_path, docs):
    remote = git_remote()
    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"]})
    billing = "draft/settlr-billing-prd"
    remote.commit(
        billing, {f"{PROJECT}/prd/settlr-billing-prd.md": "## 1. 개요\n수수료는 Admin PRD §5 참고"}
    )
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    manifest = InMemoryManifest()
    return remote, _service(remote, tmp_path, rag, manifest, counter), rag, manifest, billing


async def test_reconcile_failure_isolated_per_record_logged_and_retried_next_poll(
    git_remote, tmp_path, docs, caplog
):
    """R2-C6 · R2-T5: 참조 재해석 실패는 레코드 단위로 격리 · traceback 기록 · 다음 주기 재시도."""
    remote, service, rag, manifest, billing = _three_branch_env(git_remote, tmp_path, docs)
    billing1 = f"{billing}:{PROJECT}/prd/settlr-billing-prd.md#1"
    await service.poll_once()  # admin 문서가 아직 없다 → 두 참조 모두 미해석
    remote.commit(ADMIN_BRANCH, {ADMIN_PATH: docs["admin"]})
    rag.fail_kg_once.add(PARTNER2)
    caplog.set_level(logging.INFO)

    second = await service.poll_once()

    assert second.failed == {}
    assert (billing1, ADMIN5) in manifest.records[billing1].kg_keys.relations
    assert (PARTNER2, ADMIN5) not in manifest.records[PARTNER2].kg_keys.relations
    failed = [r for r in caplog.records if "event=cross_ref_reconcile_failed" in r.getMessage()]
    assert len(failed) == 1 and f"doc_id={PARTNER2}" in failed[0].getMessage()
    assert failed[0].exc_info is not None

    third = await service.poll_once()  # 변경 없음 — 그래도 실패분을 다시 해석한다

    assert third.indexed == [] and third.removed == []
    assert (PARTNER2, ADMIN5) in manifest.records[PARTNER2].kg_keys.relations
    assert rag.has_edge(PARTNER2, ADMIN5)


async def test_reference_added_in_later_poll_and_removed_with_target_branch(
    git_remote, tmp_path, docs
):
    """R2-T5: 참조 대상이 이후 주기에 생기면 관계를 추가하고, 대상 브랜치가 지워지면 뺀다."""
    remote, service, rag, manifest, _ = _three_branch_env(git_remote, tmp_path, docs)
    await service.poll_once()
    assert (PARTNER2, ADMIN5) not in manifest.records[PARTNER2].kg_keys.relations

    remote.commit(ADMIN_BRANCH, {ADMIN_PATH: docs["admin"]})
    await service.poll_once()
    assert (PARTNER2, ADMIN5) in manifest.records[PARTNER2].kg_keys.relations

    remote.delete_branch(ADMIN_BRANCH)
    await service.poll_once()
    assert (PARTNER2, ADMIN5) not in manifest.records[PARTNER2].kg_keys.relations
    assert not rag.has_edge(PARTNER2, ADMIN5)


async def test_glossary_failure_logged_and_retried_next_poll(env, caplog):
    """R2-T5: 용어 병합 실패는 로그로 남기고, 변경 없는 다음 주기에 다시 시도한다."""
    _, service, rag, _, _ = env
    rag.extracted_entities.update({"정산", "Settlement"})
    rag.fail_merge_once = True
    caplog.set_level(logging.INFO)

    await service.poll_once()

    assert _events(caplog, "glossary_failed")
    assert rag.merges == []

    await service.poll_once()

    assert rag.merges == [(["Settlement"], "정산")]


async def test_mutual_reference_edge_survives_reinsert_of_one_side(git_remote, tmp_path):
    """C9: 무방향 엣지 — 한쪽 챕터를 다시 넣어도 상대 챕터가 소유한 엣지는 남는다."""
    remote = git_remote()
    path = f"{PROJECT}/prd/settlr-m-prd.md"
    branch = "draft/settlr-m-prd"
    ch1, ch2 = f"{branch}:{path}#1", f"{branch}:{path}#2"
    remote.commit(branch, {path: "## 1. 하나\n§2 참고\n\n## 2. 둘\n§1 참고\n"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    service = _service(remote, tmp_path, rag, InMemoryManifest(), counter)
    await service.poll_once()
    assert rag.has_edge(ch1, ch2)

    remote.commit(branch, {path: "## 1. 하나\n참조 없음\n\n## 2. 둘\n§1 참고\n"})
    await service.poll_once()

    assert rag.has_edge(ch1, ch2)


async def test_reference_to_missing_section_gets_edge_only_after_section_appears(
    git_remote, tmp_path
):
    """C14: 없는 § 로는 엣지(placeholder)를 만들지 않고, 그 챕터가 생기면 재해석으로 잇는다."""
    remote = git_remote()
    path = f"{PROJECT}/prd/settlr-m-prd.md"
    branch = "draft/settlr-m-prd"
    ch1, ch9 = f"{branch}:{path}#1", f"{branch}:{path}#9"
    remote.commit(branch, {path: "## 1. 하나\n§9 참고\n"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    manifest = InMemoryManifest()
    service = _service(remote, tmp_path, rag, manifest, counter)

    await service.poll_once()

    assert not rag.has_edge(ch1, ch9)
    assert ch9 not in {r["tgt_id"] for r in rag.kg[ch1].custom_kg["relationships"]}

    remote.commit(branch, {path: "## 1. 하나\n§9 참고\n\n## 9. 아홉\n본문\n"})
    await service.poll_once()

    assert rag.has_edge(ch1, ch9)
    assert (ch1, ch9) in manifest.records[ch1].kg_keys.relations


async def test_extract_event_logs_reference_counts(env, caplog):
    """T8: M7 이 의존하는 event=extract 필드를 고정한다."""
    _, service, _, _, _ = env
    caplog.set_level(logging.INFO)

    await service.poll_once()

    line = next(ln for ln in _events(caplog, "extract") if f"doc_id={PARTNER2} " in ln)
    assert _field(line, "screens") == "1"
    assert _field(line, "policies") == "1"
    assert _field(line, "section_refs") == "0"
    assert _field(line, "cross_doc_refs") == "1"


def test_service_depends_on_git_source_port_not_concrete_adapter():
    import typing

    from specgraph.indexer.git_source import GitSourcePort

    hints = typing.get_type_hints(IndexerService.__init__)

    assert hints["git"] is GitSourcePort


async def test_git_listing_failure_returns_without_changes(tmp_path):
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    manifest = InMemoryManifest(heads={"draft/a": "x"})
    git = GitSource(repo_url=(tmp_path / "none.git").as_uri(), cache_dir=tmp_path / "m", token=None)
    service = IndexerService(git=git, rag=rag, manifest=manifest, counter=counter)

    result = await service.poll_once()

    assert result.error is not None
    assert manifest.heads == {"draft/a": "x"}
    assert rag.calls == []


async def test_relation_delete_failure_keeps_manifest_kg_keys_and_retries_next_poll(env, docs):
    """R3-C2 (AC7): 관계 삭제가 실패한 챕터의 완료 kg_keys 는 바뀌지 않고, 다음 주기에 정리된다."""
    remote, service, rag, manifest, _ = env
    await service.poll_once()
    completed = manifest.records[PARTNER2].kg_keys
    assert rag.has_edge(PARTNER2, "PTN-P-04")
    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("PTN-P-04", "PTN-P-05")})
    rag.fail_relation_delete_once = True

    first = await service.poll_once()

    assert list(first.failed) == [PARTNER_BRANCH]
    pending = manifest.records[PARTNER2]
    assert pending.content_hash == ""  # 완료 기록이 아니다
    assert set(completed.relations) <= set(pending.kg_keys.relations)  # 옛 관계를 계속 기억한다
    assert rag.has_edge(PARTNER2, "PTN-P-04")
    assert manifest.heads[PARTNER_BRANCH] != remote.shas[PARTNER_BRANCH]  # HEAD 미갱신

    second = await service.poll_once()

    assert second.failed == {}
    assert not rag.has_edge(PARTNER2, "PTN-P-04")
    assert rag.has_edge(PARTNER2, "PTN-P-05")
    assert manifest.records[PARTNER2].content_hash


async def test_reconcile_refs_relation_delete_failure_leaves_kg_keys_unchanged(
    git_remote, tmp_path, docs, caplog
):
    """R3-C2 (AC7): 참조 재해석 중 관계 삭제가 실패하면 그 kg_keys 는 그대로고 재시도된다."""
    remote, service, rag, manifest, billing = _three_branch_env(git_remote, tmp_path, docs)
    await service.poll_once()  # admin 문서가 아직 없다 → 참조 미해석
    before = {d: r.kg_keys for d, r in manifest.records.items()}
    remote.commit(ADMIN_BRANCH, {ADMIN_PATH: docs["admin"]})
    rag.fail_relation_delete_once = True
    caplog.set_level(logging.INFO)

    second = await service.poll_once()

    assert second.failed == {}
    failed = [r for r in caplog.records if "event=cross_ref_reconcile_failed" in r.getMessage()]
    assert len(failed) == 1
    failed_doc = _field(failed[0].getMessage(), "doc_id")
    assert manifest.records[failed_doc].kg_keys == before[failed_doc]

    third = await service.poll_once()  # 변경 없음 — 그래도 실패분을 다시 해석한다

    assert third.failed == {}
    assert manifest.records[failed_doc].kg_keys != before[failed_doc]
    assert rag.has_edge(PARTNER2, ADMIN5)


async def test_reinsert_failure_keeps_previous_content_and_shas_in_manifest(env, docs):
    """R3-C4 (AC2): 재삽입 시작 기록이 직전 완료본의 본문 · SHA 를 덮어쓰지 않는다."""
    from specgraph.mcp_server.lookup import LookupService

    remote, service, rag, manifest, _ = env
    await service.poll_once()
    done = manifest.records[ADMIN5]
    remote.commit(ADMIN_BRANCH, {ADMIN_PATH: docs["admin"].replace("D+2 이며", "D+3 이며")})
    rag.fail_once.add(ADMIN5)

    result = await service.poll_once()

    assert list(result.failed) == [ADMIN_BRANCH]
    pending = manifest.records[ADMIN5]
    assert pending.content_hash == ""
    assert pending.content == done.content
    assert pending.commit_sha == done.commit_sha
    assert pending.content_commit_sha == done.content_commit_sha
    assert pending.screens == done.screens and pending.policies == done.policies
    assert pending.is_pending and not pending.is_hidden
    shown = await LookupService(manifest).get_chapter(f"{ADMIN_BRANCH}:{ADMIN_PATH}", "5")
    assert shown["content"] == done.content and shown["commit_sha"] == done.commit_sha


async def test_first_insert_pending_record_is_hidden_without_content_commit_sha(env):
    """R3-C4 (AC1): 첫 삽입 시작 기록은 content_commit_sha 가 비어 숨김 대상이 된다."""
    from specgraph.mcp_server.lookup import LookupService

    _, service, rag, manifest, _ = env
    target = f"{PARTNER_BRANCH}:{PARTNER_PATH}#2"
    rag.fail_once.add(target)

    await service.poll_once()

    pending = manifest.records[target]
    assert pending.is_pending and pending.content_commit_sha == ""
    assert pending.is_hidden
    listed = await LookupService(manifest).list_docs()
    shown = {c["doc_id"] for b in listed["branches"] for d in b["documents"] for c in d["chapters"]}
    assert target not in shown


async def test_insert_failure_then_content_change_retry_deletes_intermediate_anchor_chunk(
    env, docs
):
    """R3-C3 (AC10): 완료 기록이 실패한 시도의 앵커 청크가, 내용이 또 바뀐 재시도에서 지워진다."""
    remote, service, rag, manifest, _ = env
    await service.poll_once()
    manifest.fail_commit_upsert_once.add(PARTNER2)
    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("PTN-P-04", "PTN-P-05")})
    first = await service.poll_once()
    assert list(first.failed) == [PARTNER_BRANCH]
    assert len(rag.anchors[PARTNER2]) == 1  # 중간 시도의 앵커

    remote.commit(PARTNER_BRANCH, {PARTNER_PATH: docs["partner"].replace("PTN-P-04", "PTN-P-06")})
    second = await service.poll_once()

    assert second.failed == {}
    final = manifest.records[PARTNER2].kg_keys
    assert rag.anchors[PARTNER2] == {final.anchor_content}
    assert final.stale_anchors == ()


async def test_mutual_reference_edge_survives_reinsert_of_second_chapter(git_remote, tmp_path):
    """R3-T2 (AC12): 챕터 2 쪽(역방향 소유자)을 다시 넣어도 챕터 1 소유 엣지는 남는다."""
    remote = git_remote()
    path = f"{PROJECT}/prd/settlr-m-prd.md"
    branch = "draft/settlr-m-prd"
    ch1, ch2 = f"{branch}:{path}#1", f"{branch}:{path}#2"
    remote.commit(branch, {path: "## 1. 하나\n§2 참고\n\n## 2. 둘\n§1 참고\n"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    service = _service(remote, tmp_path, rag, InMemoryManifest(), counter)
    await service.poll_once()
    assert rag.has_edge(ch1, ch2)

    remote.commit(branch, {path: "## 1. 하나\n§2 참고\n\n## 2. 둘\n참조 없음\n"})
    await service.poll_once()

    assert rag.has_edge(ch1, ch2)


async def test_sync_logs_chapter_insert_for_project_scoped_doc(git_remote, tmp_path, caplog):
    """PPS-348 AC10: `<프로젝트>/<대상 디렉터리>/…` 문서의 챕터가 insert 로그와 함께 색인된다."""
    remote = git_remote()
    path = "settlr/prd/admin-prd.md"
    branch = "draft/settlr-admin-prd"
    remote.commit(branch, {path: "## 1. 개요\n본문"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    service = _service(remote, tmp_path, rag, InMemoryManifest(), counter)
    caplog.set_level(logging.INFO)

    await service.poll_once()

    inserts = [ln for ln in _events(caplog, "chapter_sync") if "action=insert" in ln]
    assert len(inserts) == 1 and f"doc_id={branch}:{path}#1 " in inserts[0]
    assert rag.doc_ids(f"{branch}:") == [f"{branch}:{path}#1"]


async def test_branch_without_target_files_ends_with_zero_inserted_and_no_error(
    git_remote, tmp_path, caplog
):
    """PPS-348 AC12: 대상 문서가 없는 브랜치는 오류 없이 inserted=0 으로 끝난다."""
    remote = git_remote()
    branch = "draft/settlr-readme"
    remote.commit(branch, {"settlr/README.md": "# readme"})
    counter = LlmCallCounter()
    rag = FakeLightRAG(counter=counter)
    service = _service(remote, tmp_path, rag, InMemoryManifest(), counter)
    caplog.set_level(logging.INFO)

    result = await service.poll_once()

    assert result.failed == {}
    done = _events(caplog, "sync_done")
    assert len(done) == 1 and f"branch={branch} " in done[0] and "inserted=0" in done[0]
    assert rag.doc_ids() == []
