"""블록 분할 (AC10 — 결정 D7). tests/indexer/test_chapters.py 에서 옮겼다(공유 모듈로 이동, A1)."""

from specgraph.blocks import BLOCK_SEPARATOR, join_blocks, split_blocks


def test_blocks_keep_tables_whole_under_limit():
    table = "\n".join(
        ["| 화면 ID | 이름 |", "|---|---|"] + [f"| ADM-{i:02d} | 화면 {i} |" for i in range(10)]
    )
    text = "## 1. 화면\n\n" + "가" * 50 + "\n\n" + table + "\n\n" + "나" * 50

    blocks = split_blocks(text, max_chars=len(table) + 10)

    assert table in blocks
    assert all(len(b) <= len(table) + 10 for b in blocks)
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.startswith("|")]
        assert not lines or lines[0] == "| 화면 ID | 이름 |"


def test_small_units_are_packed_into_one_block():
    text = "## 1. 제목\n\n짧은 문단 하나\n\n짧은 문단 둘"

    assert split_blocks(text, max_chars=1000) == [text]


def test_oversized_table_split_on_row_boundary_with_header_repeated():
    header = "| 정책 ID | 내용 |\n|---|---|"
    rows = [f"| P-{i}.1 | {'내용' * 10} |" for i in range(40)]
    table = header + "\n" + "\n".join(rows)

    blocks = split_blocks(table, max_chars=300)

    assert len(blocks) > 1
    seen_rows = []
    for block in blocks:
        assert block.startswith(header + "\n")
        assert len(block) <= 300
        body = block.splitlines()[2:]
        assert all(line in rows for line in body)
        seen_rows.extend(body)
    assert seen_rows == rows


def test_oversized_table_split_by_custom_measure_on_row_boundary():
    header = "| 정책 ID | 내용 |\n|---|---|"
    rows = [f"| P-{i}.1 | {'정산 정책 설명' * 3} |" for i in range(30)]
    table = header + "\n" + "\n".join(rows)

    def utf8(text: str) -> int:
        return len(text.encode("utf-8"))

    assert len(table) < 1200 < utf8(table)
    blocks = split_blocks(table, max_chars=1200, measure=utf8)

    assert len(blocks) > 1
    seen_rows = []
    for block in blocks:
        assert block.startswith(header + "\n")
        assert utf8(block) <= 1200
        seen_rows.extend(block.splitlines()[2:])
    assert seen_rows == rows


def test_pipeless_gfm_table_is_a_table_split_on_row_boundary_with_header():
    header = "정책 ID | 내용\n--- | ---"
    rows = [f"P-{i}.1 | {'내용' * 10}" for i in range(40)]
    text = "앞 문단\n" + header + "\n" + "\n".join(rows) + "\n\n뒤 문단"

    blocks = split_blocks(text, max_chars=300)

    table_blocks = [b for b in blocks if "P-" in b]
    assert len(table_blocks) > 1
    seen_rows = []
    for block in table_blocks:
        assert header + "\n" in block  # 조각마다 헤더 반복
        assert len(block) <= 300
        seen_rows.extend(ln for ln in block.splitlines() if ln in rows)
    assert seen_rows == rows
    assert blocks[0].startswith("앞 문단") and blocks[-1].endswith("뒤 문단")


def test_code_fence_kept_whole_in_blocks():
    fence = "```\nline1\n\nline2\n```"
    text = "앞 문단\n\n" + fence + "\n\n뒤 문단"

    blocks = split_blocks(text, max_chars=len(fence))

    assert fence in blocks


def test_join_blocks_uses_unique_separator():
    joined = join_blocks(["a", "b"])

    assert joined == f"a{BLOCK_SEPARATOR}b"
    assert BLOCK_SEPARATOR.strip() and "|" not in BLOCK_SEPARATOR
