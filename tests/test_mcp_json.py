import json
from pathlib import Path

ROOT = Path(__file__).parents[1]

REQUIRED_ENV = {
    "SPECGRAPH_REPO_URL",
    "LLM_MODEL",
    "LLM_BINDING_HOST",
    "EMBEDDING_MODEL",
    "EMBEDDING_BINDING_HOST",
    "RERANK_MODEL",
    "RERANK_BINDING_HOST",
    "SUMMARY_LANGUAGE",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "LIGHTRAG_KV_STORAGE",
    "LIGHTRAG_VECTOR_STORAGE",
    "LIGHTRAG_GRAPH_STORAGE",
    "LIGHTRAG_DOC_STATUS_STORAGE",
}


def _server():
    data = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    return data["mcpServers"]["specgraph"]


def test_mcp_json_registers_specgraph_stdio_server():
    server = _server()

    assert server["type"] == "stdio"
    assert server["command"] == "uv"
    assert server["args"][-1] == "specgraph-mcp"
    assert "--directory" in server["args"]
    assert REQUIRED_ENV.issubset(server["env"])


def test_mcp_json_targets_local_services_only():
    env = _server()["env"]

    assert "localhost" in env["POSTGRES_HOST"]
    for key in ("LLM_BINDING_HOST", "EMBEDDING_BINDING_HOST", "RERANK_BINDING_HOST"):
        assert "localhost" in env[key] or "127.0.0.1" in env[key]


def test_mcp_json_has_no_secret_literals():
    """S8: 비밀번호는 기본값 없이 셸 환경변수에서만 온다(없으면 Claude Code 가 설정을 거부)."""
    env = _server()["env"]

    assert env["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD}"
    assert "SPECGRAPH_GIT_TOKEN" not in env


def test_mcp_json_model_names_have_no_literal_defaults():
    """A7: 모델명의 출처는 deploy/.env(.env.example) 하나다. .mcp.json 에 기본값을 두지 않는다."""
    env = _server()["env"]

    for key in ("LLM_MODEL", "EMBEDDING_MODEL", "RERANK_MODEL"):
        assert env[key] == f"${{{key}}}"
