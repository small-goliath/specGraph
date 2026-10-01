import ast
import inspect

from specgraph.indexer import kg
from specgraph.indexer.extract import DocumentRef, extract_chapter
from specgraph.indexer.kg import build_chapter_kg
from specgraph.kg_model import KgKeys

ADMIN = DocumentRef(branch="draft/settlr-admin-prd", path="prd/settlr-admin-prd.md")
PARTNER = DocumentRef(branch="draft/settlr-partner-prd", path="prd/settlr-partner-prd.md")
DOCS = [ADMIN, PARTNER]

PARTNER_CH2 = "draft/settlr-partner-prd:prd/settlr-partner-prd.md#2"
ADMIN_CH5 = "draft/settlr-admin-prd:prd/settlr-admin-prd.md#5"
ADMIN_CH3 = "draft/settlr-admin-prd:prd/settlr-admin-prd.md#3"

PARTNER_TEXT = (
    "## 2. PTN-P-04 정산 조회\n\n"
    "파트너는 PTN-P-04 화면에서 조회한다. 로그인은 AUTH-01.\n"
    "정산 주기는 Admin PRD §5 를 따른다 (P-14.3)."
)
ADMIN_TEXT = "## 3. ADM-03 정산 내역\n\n조회 조건은 §5.2 를 따른다. 자기 참조 §3."


def _build(doc_id, text, title):
    return build_chapter_kg(doc_id, title, extract_chapter(text), DOCS)


def _entities(payload):
    return {e["entity_name"]: e for e in payload.custom_kg["entities"]}


def _relations(payload):
    return {(r["src_id"], r["tgt_id"]): r for r in payload.custom_kg["relationships"]}


def test_payload_contains_screen_policy_chapter_entities():
    payload = _build(PARTNER_CH2, PARTNER_TEXT, "2. PTN-P-04 정산 조회")
    entities = _entities(payload)

    assert entities[PARTNER_CH2]["entity_type"] == "CHAPTER"
    assert (
        entities["draft/settlr-partner-prd:prd/settlr-partner-prd.md"]["entity_type"] == "DOCUMENT"
    )
    assert entities["PTN-P-04"]["entity_type"] == "SCREEN"
    assert entities["AUTH-01"]["entity_type"] == "SCREEN"
    assert entities["P-14.3"]["entity_type"] == "POLICY"


def test_relations_defines_mentions_refers_to():
    payload = _build(PARTNER_CH2, PARTNER_TEXT, "2. PTN-P-04 정산 조회")
    rels = _relations(payload)

    assert rels[(PARTNER_CH2, "PTN-P-04")]["keywords"] == "DEFINES"
    assert rels[(PARTNER_CH2, "AUTH-01")]["keywords"] == "MENTIONS"
    assert rels[(PARTNER_CH2, "P-14.3")]["keywords"] == "MENTIONS"
    assert rels[(PARTNER_CH2, ADMIN_CH5)]["keywords"] == "REFERS_TO"
    assert (
        rels[(PARTNER_CH2, "draft/settlr-partner-prd:prd/settlr-partner-prd.md")]["keywords"]
        == "PART_OF"
    )


def test_same_document_section_refs_become_refers_to_without_self_loop():
    payload = _build(ADMIN_CH3, ADMIN_TEXT, "3. ADM-03 정산 내역")
    rels = _relations(payload)

    assert rels[(ADMIN_CH3, ADMIN_CH5)]["keywords"] == "REFERS_TO"
    assert (ADMIN_CH3, ADMIN_CH3) not in rels


def test_unresolved_cross_doc_ref_has_no_edge_and_is_reported():
    text = "## 1. 개요\nBilling PRD §2 참고"
    payload = _build("draft/settlr-partner-prd:prd/settlr-partner-prd.md#1", text, "1. 개요")

    assert [r.name for r in payload.unresolved] == ["Billing"]
    assert all(r["keywords"] != "REFERS_TO" for r in payload.custom_kg["relationships"])


def test_refs_to_unknown_chapters_get_no_edge_and_are_reported_dangling():
    """C14: 없는 챕터로의 참조는 엣지를 만들지 않는다(LightRAG placeholder 노드 방지)."""
    known = {ADMIN_CH3, PARTNER_CH2}

    admin = build_chapter_kg(
        ADMIN_CH3, "3. ADM-03 정산 내역", extract_chapter(ADMIN_TEXT), DOCS, chapters=known
    )
    partner = build_chapter_kg(
        PARTNER_CH2, "2. PTN-P-04 정산 조회", extract_chapter(PARTNER_TEXT), DOCS, chapters=known
    )

    assert (ADMIN_CH3, ADMIN_CH5) not in _relations(admin)
    assert admin.dangling == [ADMIN_CH5]
    assert (PARTNER_CH2, ADMIN_CH5) not in _relations(partner)
    assert partner.dangling == [ADMIN_CH5]
    assert set(admin.keys.relations) == set(_relations(admin))


def test_refs_to_known_chapters_keep_edges_when_chapter_set_given():
    known = {ADMIN_CH3, ADMIN_CH5, PARTNER_CH2}

    payload = build_chapter_kg(
        PARTNER_CH2, "2. PTN-P-04 정산 조회", extract_chapter(PARTNER_TEXT), DOCS, chapters=known
    )

    assert _relations(payload)[(PARTNER_CH2, ADMIN_CH5)]["keywords"] == "REFERS_TO"
    assert payload.dangling == []


def test_all_source_ids_are_chapter_anchor_chunk():
    payload = _build(PARTNER_CH2, PARTNER_TEXT, "2. PTN-P-04 정산 조회")
    chunks = payload.custom_kg["chunks"]

    assert len(chunks) == 1
    assert chunks[0]["source_id"] == PARTNER_CH2
    assert chunks[0]["file_path"] == PARTNER_CH2
    assert PARTNER_CH2 in chunks[0]["content"]
    for item in payload.custom_kg["entities"] + payload.custom_kg["relationships"]:
        assert item["source_id"] == PARTNER_CH2
        assert item["file_path"] == PARTNER_CH2


def test_keys_record_owned_relations_and_chapter_entity():
    payload = _build(PARTNER_CH2, PARTNER_TEXT, "2. PTN-P-04 정산 조회")

    assert payload.keys.chapter_entity == PARTNER_CH2
    assert set(payload.keys.relations) == set(_relations(payload))
    assert payload.keys.anchor_content == payload.custom_kg["chunks"][0]["content"]


def test_keys_json_roundtrip():
    keys = KgKeys(chapter_entity="a#1", relations=(("a#1", "X-01"),), anchor_content="c")

    assert KgKeys.from_json(keys.to_json()) == keys


def test_builder_makes_no_llm_calls():
    tree = ast.parse(inspect.getsource(kg))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any(m.startswith(("lightrag", "specgraph.llm", "ollama")) for m in imported)
    assert not inspect.iscoroutinefunction(build_chapter_kg)
