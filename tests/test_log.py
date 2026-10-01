import ast
import json
import logging
from pathlib import Path

from specgraph.log import JsonFormatter, KeyValueFormatter, log_event


def _record(caplog, **fields):
    logger = logging.getLogger("specgraph.test")
    with caplog.at_level(logging.INFO, logger="specgraph.test"):
        log_event(logger, "chapter_sync", **fields)
    return caplog.records[-1]


def test_log_event_message_is_key_value(caplog):
    record = _record(caplog, doc_id="draft/a:prd/a.md#1", action="skip", llm_calls=0)

    assert (
        record.getMessage()
        == "event=chapter_sync doc_id=draft/a:prd/a.md#1 action=skip llm_calls=0"
    )


def test_values_with_spaces_are_quoted(caplog):
    record = _record(caplog, title="정산 정책")

    assert 'title="정산 정책"' in record.getMessage()


def test_kv_formatter_one_line(caplog):
    record = _record(caplog, action="skip")

    line = KeyValueFormatter().format(record)

    assert "\n" not in line
    assert "level=INFO" in line
    assert line.endswith("event=chapter_sync action=skip")


def test_json_formatter_emits_fields(caplog):
    record = _record(caplog, doc_id="d#1", llm_calls=0)

    data = json.loads(JsonFormatter().format(record))

    assert data["event"] == "chapter_sync"
    assert data["doc_id"] == "d#1"
    assert data["llm_calls"] == 0
    assert data["level"] == "INFO"


def test_log_event_exc_info_keeps_traceback_in_both_formats(caplog):
    logger = logging.getLogger("specgraph.test")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="specgraph.test"):
            log_event(logger, "branch_failed", logging.ERROR, exc_info=exc, branch="draft/a")
    record = caplog.records[-1]

    assert record.getMessage() == "event=branch_failed branch=draft/a"
    assert "RuntimeError: boom" in KeyValueFormatter().format(record)
    assert "RuntimeError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_sources_log_only_through_log_event():
    """로그는 전부 log_event 를 거친다(JSON 모드에서 구조화 · key=value 형식 보장)."""
    root = Path(__file__).parents[1] / "src" / "specgraph"
    levels = {"debug", "info", "warning", "error", "exception", "critical", "log"}
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "log.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in levels
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert offenders == []
