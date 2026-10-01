from specgraph.indexer.chapters import split_chapters
from specgraph.indexer.glossary import Glossary, normalize_term, parse_glossary


def _glossary(fixture_docs, name):
    text = (fixture_docs / name).read_text(encoding="utf-8")
    return parse_glossary(split_chapters(text))


def test_parses_glossary_table_from_chapter_zero(fixture_docs):
    glossary = _glossary(fixture_docs, "settlr-admin-prd.md")

    assert set(glossary.terms) == {"정산", "파트너", "수수료율"}


def test_aliases_from_synonym_column_and_parentheses(fixture_docs):
    glossary = _glossary(fixture_docs, "settlr-admin-prd.md")

    assert glossary.terms["정산"] == ("세틀먼트", "Settlement")
    assert glossary.terms["파트너"] == ("Partner", "제휴사")
    assert glossary.terms["수수료율"] == ()


def test_alias_column_named_byeolching(fixture_docs):
    glossary = _glossary(fixture_docs, "settlr-partner-prd.md")

    assert glossary.terms == {"정산": ("Settlement",)}


def test_normalizes_case_and_spaces(fixture_docs):
    glossary = _glossary(fixture_docs, "settlr-admin-prd.md")

    assert normalize_term("  SETTLE  MENT ") == "settle ment"
    assert glossary.canonical_for("settlement") == "정산"
    assert glossary.canonical_for(" 제휴사 ") == "파트너"
    assert glossary.canonical_for("파트너") == "파트너"
    assert glossary.canonical_for("모르는말") is None


def test_document_without_glossary_returns_empty():
    glossary = parse_glossary(split_chapters("## 1. 개요\n본문\n"))

    assert glossary.terms == {}
    assert not glossary


def test_chapter_zero_without_table_returns_empty():
    glossary = parse_glossary(split_chapters("## 0. 용어\n표 없음\n## 1. 개요\n"))

    assert glossary.terms == {}


def test_merge_combines_aliases_across_documents(fixture_docs):
    merged = _glossary(fixture_docs, "settlr-admin-prd.md").merge(
        Glossary({"정산": ("정산처리",), "신규": ()})
    )

    assert merged.terms["정산"] == ("세틀먼트", "Settlement", "정산처리")
    assert "신규" in merged.terms


def test_merge_plan_maps_alias_entities_to_canonical(fixture_docs):
    glossary = _glossary(fixture_docs, "settlr-admin-prd.md")
    existing = ["정산", "SETTLEMENT", "세틀먼트", "제휴사", "무관 엔티티"]

    plan = glossary.merge_plan(existing)

    assert plan == [("정산", ["SETTLEMENT", "세틀먼트"]), ("파트너", ["제휴사"])]


def test_merge_plan_empty_when_no_alias_entities(fixture_docs):
    glossary = _glossary(fixture_docs, "settlr-admin-prd.md")

    assert glossary.merge_plan(["정산", "파트너"]) == []
