from specgraph.indexer.chapters import content_hash, split_chapters


def _admin(fixture_docs):
    return (fixture_docs / "settlr-admin-prd.md").read_text(encoding="utf-8")


def test_splits_on_top_level_numbered_headings(fixture_docs):
    chapters = split_chapters(_admin(fixture_docs))

    assert [c.section for c in chapters] == ["preamble", "0", "1", "2", "3", "5"]
    assert chapters[1].title == "0. 용어"
    assert chapters[4].title == "3. ADM-03 정산 내역"


def test_subsections_stay_inside_top_level_chapter(fixture_docs):
    chapter5 = split_chapters(_admin(fixture_docs))[-1]

    assert "### 5.1 정산 주기" in chapter5.text
    assert "### 5.2 수수료" in chapter5.text
    assert chapter5.text.startswith("## 5. 정산 정책")


def test_preamble_becomes_preamble_section(fixture_docs):
    preamble = split_chapters(_admin(fixture_docs))[0]

    assert preamble.section == "preamble"
    assert "# Settlr Admin PRD" in preamble.text


def test_no_preamble_when_document_starts_with_numbered_heading():
    chapters = split_chapters("## 1. 하나\n본문\n## 2. 둘\n본문\n")

    assert [c.section for c in chapters] == ["1", "2"]


def test_heading_inside_code_fence_is_not_boundary(fixture_docs):
    chapters = split_chapters(_admin(fixture_docs))

    assert "9" not in [c.section for c in chapters]
    chapter2 = next(c for c in chapters if c.section == "2")
    assert "## 9. 코드펜스 안의 헤딩은 챕터 경계가 아니다" in chapter2.text


def test_heading_level_follows_first_numbered_heading():
    doc = "# 1. 첫째\n## 1.1 하위\n## 2. 같은 레벨 아님\n# 2. 둘째\n"

    chapters = split_chapters(doc)

    assert [c.section for c in chapters] == ["1", "2"]
    assert "## 2. 같은 레벨 아님" in chapters[0].text


def test_section_sign_heading_forms():
    doc = "## §1 하나\nA\n## § 2. 둘\nB\n## 3 셋\nC\n"

    assert [c.section for c in split_chapters(doc)] == ["1", "2", "3"]


def test_duplicate_section_number_is_merged_into_previous_chapter():
    doc = "## 1. 하나\nA\n## 1. 다시 하나\nB\n## 2. 둘\nC\n"

    chapters = split_chapters(doc)

    assert [c.section for c in chapters] == ["1", "2"]
    assert "B" in chapters[0].text


def test_table_is_never_split_by_chapter_boundary():
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n| ## 3. 표 셀 | x |"
    doc = f"## 1. 표 앞\n{table}\n## 2. 표 뒤\n본문\n"

    chapters = split_chapters(doc)

    assert [c.section for c in chapters] == ["1", "2"]
    assert table in chapters[0].text


def test_content_hash_stable_for_whitespace_only_change():
    base = "## 1. 제목\n본문 첫 줄\n\n본문 둘째 줄"
    noisy = "## 1. 제목   \n본문 첫 줄\t\n\n\n\n본문 둘째 줄\n\n"

    assert content_hash(base) == content_hash(noisy)
    assert content_hash(base) != content_hash(base.replace("둘째", "셋째"))
    assert len(content_hash(base)) == 64


def test_chapter_content_hash_matches_function(fixture_docs):
    chapter = split_chapters(_admin(fixture_docs))[1]

    assert chapter.content_hash == content_hash(chapter.text)
