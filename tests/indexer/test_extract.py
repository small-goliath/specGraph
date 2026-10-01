import pytest

from specgraph.indexer.extract import (
    DocumentRef,
    definition_rank,
    extract_chapter,
    extract_cross_doc_refs,
    extract_policy_ids,
    extract_screen_ids,
    extract_section_refs,
    resolve_cross_doc_ref,
)
from specgraph.kg_model import (
    DEFINED_IN_HEADING,
    DEFINED_IN_TABLE,
    MENTIONED,
    CrossDocRef,
    choose_definition,
)

DOCS = [
    DocumentRef(branch="draft/settlr-admin-prd", path="prd/settlr-admin-prd.md"),
    DocumentRef(branch="draft/settlr-partner-prd", path="prd/settlr-partner-prd.md"),
    DocumentRef(branch="draft/settlr-admin-uiux", path="ui-ux-spec/settlr-admin-uiux.md"),
]


@pytest.mark.parametrize("screen", ["ADM-03", "PTN-P-04", "AUTH-01"])
def test_extracts_screen_ids(screen):
    text = f"화면 {screen} 에서 조회한다. ({screen})"

    assert extract_screen_ids(text) == [screen]


def test_screen_ids_unique_in_order():
    assert extract_screen_ids("ADM-03, AUTH-01, ADM-03") == ["ADM-03", "AUTH-01"]


@pytest.mark.parametrize("text", ["ADM-1234", "1ADM-03", "ADM-03.1", "adm-03", "A-03"])
def test_screen_id_rejects_non_ids(text):
    assert extract_screen_ids(text) == []


def test_screen_id_sentence_end_dot_allowed():
    assert extract_screen_ids("로그인은 AUTH-01. 다음은 ADM-03.") == ["AUTH-01", "ADM-03"]


def test_policy_id_not_matched_inside_screen_id():
    assert extract_policy_ids("PTN-P-04 화면") == []


@pytest.mark.parametrize("policy", ["P-14.3", "U-2", "P-1"])
def test_extracts_policy_ids(policy):
    assert extract_policy_ids(f"정책 {policy} 을 따른다.") == [policy]


def test_policy_id_sentence_end_dot_not_included():
    assert extract_policy_ids("정책은 P-14.3.") == ["P-14.3"]


def test_section_refs_map_to_top_level_chapter():
    assert extract_section_refs("§5.2 와 § 3, §5 그리고 §12.1.4") == ["5", "3", "12"]


def test_section_refs_exclude_cross_doc_refs():
    assert extract_section_refs("Admin PRD §5 와 §2") == ["2"]


def test_extracts_cross_doc_refs():
    refs = extract_cross_doc_refs(
        "정산 주기는 Admin PRD §5.2 를 따른다. Partner UI/UX spec §3 참고"
    )

    assert refs == [
        CrossDocRef(name="Admin", kind="prd", section="5"),
        CrossDocRef(name="Partner", kind="ui-ux-spec", section="3"),
    ]


def test_cross_doc_ref_resolves_to_branch_doc():
    ref = CrossDocRef(name="Admin", kind="prd", section="5")

    assert resolve_cross_doc_ref(ref, DOCS) == "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"


def test_cross_doc_ref_resolves_by_kind():
    ref = CrossDocRef(name="Admin", kind="ui-ux-spec", section="2")

    assert resolve_cross_doc_ref(ref, DOCS) == (
        "draft/settlr-admin-uiux:ui-ux-spec/settlr-admin-uiux.md#2"
    )


def test_cross_doc_ref_name_with_leading_words_uses_suffix():
    ref = CrossDocRef(name="P Admin", kind="prd", section="5")

    assert resolve_cross_doc_ref(ref, DOCS) == "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"


def test_ambiguous_cross_doc_ref_is_unresolved():
    docs = DOCS + [DocumentRef(branch="draft/payout-admin-prd", path="prd/payout-admin-prd.md")]
    ref = CrossDocRef(name="Admin", kind="prd", section="5")

    assert resolve_cross_doc_ref(ref, docs) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("정산 주기는 Admin PRD의 §5.2 를 따른다", CrossDocRef("Admin", "prd", "5")),
        ("정산 주기는 Admin PRD 의 §5 를 따른다", CrossDocRef("Admin", "prd", "5")),
        ("상세는 Admin PRD에서 §3 참고", CrossDocRef("Admin", "prd", "3")),
        ("정산 주기는 어드민 PRD §5 를 따른다", CrossDocRef("어드민", "prd", "5")),
        ("화면은 파트너 UI/UX spec의 §2 참고", CrossDocRef("파트너", "ui-ux-spec", "2")),
    ],
)
def test_korean_names_and_particles_are_cross_doc_refs_not_self_refs(text, expected):
    """C7: 한글 이름 · 조사가 붙은 문서 간 참조를 같은 문서 § 참조로 오인하지 않는다."""
    assert extract_cross_doc_refs(text) == [expected]
    assert extract_section_refs(text) == []


def test_section_right_after_doc_kind_keyword_is_not_a_self_ref():
    """C7: 이름 없이 'PRD §5' 처럼 문서 종류 키워드 바로 뒤의 § 는 자기 문서 참조가 아니다."""
    text = "상세는 PRD §5 와 tech-spec 의 §2 참고, 이 문서 §3 도 본다"

    assert extract_section_refs(text) == ["3"]


def test_same_stem_on_two_branches_prefers_branch_slug_match():
    """C5: 같은 파일 stem 이 여러 브랜치에 있으면 브랜치 슬러그가 맞는 쪽을 고른다."""
    docs = [
        DocumentRef("draft/settlr-admin-prd", "prd/settlr-admin-prd.md"),
        DocumentRef("draft/settlr-partner-prd", "prd/settlr-admin-prd.md"),
        DocumentRef("draft/settlr-partner-prd", "prd/settlr-partner-prd.md"),
    ]

    resolved = resolve_cross_doc_ref(CrossDocRef("Admin", "prd", "5"), docs)

    assert resolved == "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"


def test_same_stem_and_slug_prefers_referring_branch():
    """C5: stem · 슬러그로도 동률이면 참조하는 챕터의 브랜치를 고른다."""
    docs = [
        DocumentRef("draft/a-admin-prd", "prd/admin-prd.md"),
        DocumentRef("draft/b-admin-prd", "prd/admin-prd.md"),
    ]
    ref = CrossDocRef("Admin", "prd", "5")

    assert resolve_cross_doc_ref(ref, docs, from_branch="draft/b-admin-prd") == (
        "draft/b-admin-prd:prd/admin-prd.md#5"
    )
    assert resolve_cross_doc_ref(ref, docs) is None
    assert resolve_cross_doc_ref(ref, docs, from_branch="draft/c-other") is None


def test_unknown_cross_doc_ref_is_unresolved():
    assert resolve_cross_doc_ref(CrossDocRef(name="Billing", kind="prd", section="1"), DOCS) is None


def test_screen_definition_prefers_heading_then_table_first_column():
    heading = "## 3. ADM-03 정산 내역\n본문"
    table = "| 화면 ID | 이름 |\n|---|---|\n| ADM-03 | 정산 |"
    mention = "정산 화면(ADM-03)으로 이동"

    assert definition_rank(heading, "ADM-03") == DEFINED_IN_HEADING
    assert definition_rank(table, "ADM-03") == DEFINED_IN_TABLE
    assert definition_rank(mention, "ADM-03") == MENTIONED
    assert definition_rank("무관", "ADM-03") is None
    assert DEFINED_IN_HEADING < DEFINED_IN_TABLE < MENTIONED


def test_choose_definition_lowest_rank_then_first():
    candidates = [("d#2", DEFINED_IN_TABLE), ("d#1", MENTIONED), ("d#3", DEFINED_IN_HEADING)]

    assert choose_definition(candidates) == "d#3"
    assert choose_definition([("a", MENTIONED), ("b", MENTIONED)]) == "a"
    assert choose_definition([]) is None


def test_extract_chapter_collects_all_refs(fixture_docs):
    text = (fixture_docs / "settlr-partner-prd.md").read_text(encoding="utf-8")

    refs = extract_chapter(text)

    assert refs.screens == ["AUTH-01", "PTN-P-04"]
    assert refs.policies == ["P-14.3"]
    assert refs.cross_doc_refs == [CrossDocRef(name="Admin", kind="prd", section="5")]
    assert refs.screen_ranks["PTN-P-04"] == DEFINED_IN_HEADING
    assert refs.screen_ranks["AUTH-01"] == MENTIONED
