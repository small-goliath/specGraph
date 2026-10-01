from specgraph.indexer.sync_plan import BranchDiff, ChapterPlan, diff_branches, diff_chapters


def test_branch_diff_new_changed_deleted_unchanged():
    remote = {"draft/new": "n1", "draft/changed": "c2", "draft/same": "s1"}
    known = {"draft/changed": "c1", "draft/same": "s1", "draft/gone": "g1"}

    diff = diff_branches(remote, known)

    assert diff == BranchDiff(
        new={"draft/new": "n1"},
        changed={"draft/changed": "c2"},
        deleted=["draft/gone"],
        unchanged=["draft/same"],
    )
    assert diff.to_index == {"draft/new": "n1", "draft/changed": "c2"}


def test_branch_diff_empty_known_marks_all_new():
    diff = diff_branches({"draft/a": "1", "draft/b": "2"}, {})

    assert diff.new == {"draft/a": "1", "draft/b": "2"}
    assert diff.changed == {} and diff.deleted == [] and diff.unchanged == []


def test_chapter_diff_insert_reinsert_delete_skip():
    current = {"b:p#1": "h1", "b:p#2": "h2-new", "b:p#4": "h4"}
    indexed = {"b:p#1": "h1", "b:p#2": "h2-old", "b:p#3": "h3"}

    plan = diff_chapters(current, indexed)

    assert plan == ChapterPlan(
        insert=["b:p#4"], reinsert=["b:p#2"], delete=["b:p#3"], skip=["b:p#1"]
    )
    assert plan.changed_count == 3


def test_removed_file_deletes_its_chapters():
    indexed = {"b:prd/a.md#1": "x", "b:prd/a.md#2": "y", "b:prd/b.md#1": "z"}
    current = {"b:prd/b.md#1": "z"}

    plan = diff_chapters(current, indexed)

    assert plan.delete == ["b:prd/a.md#1", "b:prd/a.md#2"]
    assert plan.skip == ["b:prd/b.md#1"]
    assert plan.insert == [] and plan.reinsert == []


def test_identical_chapters_all_skip():
    state = {"b:p#1": "h1", "b:p#2": "h2"}

    plan = diff_chapters(state, dict(state))

    assert plan.skip == ["b:p#1", "b:p#2"]
    assert plan.changed_count == 0
