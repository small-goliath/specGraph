import pytest

from specgraph.docid import (
    DocId,
    belongs_to_branch,
    branch_prefix,
    build_doc_id,
    document_key,
    parse_doc_id,
)


def test_build_doc_id_formats_branch_path_section():
    doc_id = build_doc_id("draft/settlr-admin-prd", "prd/settlr-admin-prd.md", "5")

    assert doc_id == "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"


def test_build_doc_id_preamble_section():
    assert build_doc_id("draft/a", "prd/a.md", "preamble") == "draft/a:prd/a.md#preamble"


def test_parse_doc_id_roundtrip():
    doc_id = "draft/settlr-partner-prd:prd/sub/partner.md#12"

    parsed = parse_doc_id(doc_id)

    assert parsed == DocId(
        branch="draft/settlr-partner-prd", path="prd/sub/partner.md", section="12"
    )
    assert parsed.value == doc_id
    assert build_doc_id(parsed.branch, parsed.path, parsed.section) == doc_id


@pytest.mark.parametrize("bad", ["", "no-colon#1", "draft/a:prd/a.md", ":prd/a.md#1", "draft/a:#1"])
def test_parse_doc_id_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_doc_id(bad)


def test_branch_prefix_matches_only_same_branch():
    doc = build_doc_id("draft/ab", "prd/x.md", "1")

    assert branch_prefix("draft/a") == "draft/a:"
    assert not belongs_to_branch(doc, "draft/a")
    assert belongs_to_branch(doc, "draft/ab")


def test_document_key_drops_section():
    assert document_key("draft/a:prd/a.md#3") == "draft/a:prd/a.md"
