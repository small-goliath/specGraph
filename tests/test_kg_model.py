import json

from specgraph.kg_model import KgKeys


def test_kg_keys_from_json_without_stale_anchors_defaults_empty():
    """구버전 DB 의 kg_keys JSON(stale_anchors 키 없음)도 읽는다."""
    raw = json.dumps({"chapter_entity": "c", "relations": [["c", "x"]], "anchor_content": "a"})

    keys = KgKeys.from_json(raw)

    assert keys == KgKeys("c", (("c", "x"),), "a")
    assert keys.stale_anchors == ()


def test_kg_keys_json_roundtrip_keeps_stale_anchors():
    keys = KgKeys("c", (("c", "x"),), "a", stale_anchors=("m1", "m2"))

    assert KgKeys.from_json(keys.to_json()) == keys
    assert json.loads(keys.to_json())["stale_anchors"] == ["m1", "m2"]
