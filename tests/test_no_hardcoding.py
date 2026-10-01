"""회귀 가드 (AC2 · AC4 · 외부 LLM API 금지 제약).

모델명 · 브랜치명 · 외부 LLM 호스트가 코드 · compose · .mcp.json 에 박히면 실패한다.
모델명의 허용 위치는 deploy/.env.example(템플릿) 하나뿐이다(A7 — 설정 출처 단일화).
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SOURCES = sorted((ROOT / "src").rglob("*.py"))
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
MCP_JSON = ROOT / ".mcp.json"

MODEL_LITERALS = ("qwen", "bge-m3", "bge-reranker", "kure", "exaone")
EXTERNAL_LLM_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.cohere.com",
    "api.jina.ai",
    "openrouter.ai",
)


def _files():
    return [*SOURCES, COMPOSE, MCP_JSON]


def test_sources_exist():
    assert len(SOURCES) > 10


@pytest.mark.parametrize("literal", MODEL_LITERALS)
def test_no_model_literals_in_source_and_compose(literal):
    offenders = [p.name for p in _files() if literal in p.read_text(encoding="utf-8").lower()]

    assert offenders == []


@pytest.mark.parametrize("host", EXTERNAL_LLM_HOSTS)
def test_no_external_llm_hosts(host):
    offenders = [p.name for p in _files() if host in p.read_text(encoding="utf-8").lower()]

    assert offenders == []


def test_no_branch_literals_in_source():
    offenders = [p.name for p in SOURCES if "settlr" in p.read_text(encoding="utf-8").lower()]

    assert offenders == []
