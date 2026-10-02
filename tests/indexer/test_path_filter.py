import pytest

from specgraph.indexer.path_filter import filter_paths, is_indexable
from specgraph.settings import DEFAULT_INCLUDE_DIRS

DIRS = ("prd", "ui-ux-spec", "tech-spec", "qa")


@pytest.mark.parametrize(
    "path",
    [
        "settlr/prd/admin-prd.md",
        "settlr/ui-ux-spec/admin-ui-ux-spec.md",
        "paycoin-app/prd/daily-mission-prd.md",
        "settlr/tech-spec/api.md",
        "settlr/qa/qa.md",
        "settlr/prd/UPPER.MD",
        "x/prd/a.md",
    ],
)
def test_includes_project_scoped_md_under_target_dirs(path):
    assert is_indexable(path, DIRS)


@pytest.mark.parametrize(
    "path",
    ["settlr/prd/nested/doc.md", "settlr/prd/nested/deeper/doc.md"],
)
def test_includes_nested_path_under_project_target_dir(path):
    assert is_indexable(path, DIRS)


def test_project_named_like_target_dir_is_included():
    assert is_indexable("prd/prd/a.md", DIRS)


@pytest.mark.parametrize(
    "path",
    [
        "settlr/README.md",
        "settlr/CLAUDE.md",
        "settlr/prd/README.md",
        "settlr/prd/CLAUDE.md",
    ],
)
def test_excludes_readme_claude_files_under_project(path):
    assert not is_indexable(path, DIRS)


@pytest.mark.parametrize(
    "path",
    ["settlr/prd/doc.docx", "settlr/prd/images/screen.png", "settlr/prd/notes.txt"],
)
def test_excludes_non_md_files_under_project(path):
    assert not is_indexable(path, DIRS)


@pytest.mark.parametrize(
    "path",
    ["settlr/docs/x.md", "settlr/prdx/x.md", "x.md", "prd/a.md", "settlr/prd.md"],
)
def test_excludes_md_outside_target_dirs_under_project(path):
    assert not is_indexable(path, DIRS)


def test_excludes_md_with_extra_directory_before_target_dir():
    assert not is_indexable("a/b/prd/c.md", DIRS)


def test_include_dirs_are_configurable():
    assert is_indexable("proj/design/a.md", ("design",))
    assert not is_indexable("proj/prd/a.md", ("design",))


def test_default_include_dirs_unchanged():
    assert DEFAULT_INCLUDE_DIRS == ("prd", "ui-ux-spec", "tech-spec", "qa")


def test_filter_paths_keeps_order_and_drops_excluded():
    paths = ["README.md", "settlr/prd/b.md", "settlr/prd/a.png", "settlr/qa/a.md"]

    assert filter_paths(paths, DIRS) == ["settlr/prd/b.md", "settlr/qa/a.md"]
