"""구조적 로깅 — 표준 logging 위에 한 줄 key=value(기본) 또는 JSON.

``log_event(logger, "chapter_sync", doc_id=…, action=…, llm_calls=…)`` 로 남긴다.
메시지 자체가 ``event=… k=v`` 형식이라 어떤 포매터로도 grep 할 수 있다(AC9 로그 검증).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_FIELDS_ATTR = "specgraph_fields"


def _kv_value(value: Any) -> str:
    text = str(value)
    if text == "" or any(ch.isspace() for ch in text) or '"' in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def format_fields(fields: dict[str, Any]) -> str:
    return " ".join(f"{key}={_kv_value(value)}" for key, value in fields.items())


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    *,
    exc_info: BaseException | None = None,
    **fields: Any,
) -> None:
    """``exc_info`` 를 주면 traceback 을 함께 남긴다(kv 는 ``exc=…``, JSON 은 ``exc`` 필드)."""
    payload = {"event": event, **fields}
    logger.log(
        level, "%s", format_fields(payload), exc_info=exc_info, extra={_FIELDS_ATTR: payload}
    )


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        head = format_fields(
            {"ts": _timestamp(record), "level": record.levelname, "logger": record.name}
        )
        line = f"{head} {record.getMessage()}".replace("\n", "\\n")
        if record.exc_info:
            line += " exc=" + _kv_value(self.formatException(record.exc_info))
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
        }
        fields = getattr(record, _FIELDS_ATTR, None)
        if fields:
            data.update(fields)
        else:
            data["message"] = record.getMessage()
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging(fmt: str = "kv", level: str = "INFO") -> None:
    """stderr 로 출력한다(MCP stdio 의 stdout 을 오염시키지 않는다)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
