import pytest

from specgraph.indexer.path_filter import filter_paths, is_indexable

DIRS = ("prd", "ui-ux-spec", "tech-spec", "qa")


@pytest.mark.parametrize(
    "path",
    [
        "prd/settlr-admin-prd.md",
        "ui-ux-spec/settlr-admin-uiux.md",
        "tech-spec/settlr-api.md",
        "qa/settlr-qa.md",
        "prd/nested/deeper/doc.md",
        "prd/UPPER.MD",
    ],
)
def test_includes_md_under_target_dirs(path):
    assert is_indexable(path, DIRS)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CLAUDE.md",
        "prd/README.md",
        "prd/CLAUDE.md",
        "prd/settlr-admin-prd.docx",
        "prd/images/screen.png",
        "prd/notes.txt",
    ],
)
def test_excludes_readme_claude_docx_png(path):
    assert not is_indexable(path, DIRS)


@pytest.mark.parametrize("path", ["docs/x.md", "prdx/x.md", "x/prd/a.md", "main.md"])
def test_excludes_md_outside_target_dirs(path):
    assert not is_indexable(path, DIRS)


def test_include_dirs_are_configurable():
    assert is_indexable("design/a.md", ("design",))
    assert not is_indexable("prd/a.md", ("design",))


def test_filter_paths_keeps_order_and_drops_excluded():
    paths = ["README.md", "prd/b.md", "prd/a.png", "qa/a.md"]

    assert filter_paths(paths, DIRS) == ["prd/b.md", "qa/a.md"]
