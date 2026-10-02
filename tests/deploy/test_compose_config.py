"""deploy/ 정적 검증 — docker-compose.yml · .env.example · Postgres init (AC1 · AC2 · AC3)."""

import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).parents[2] / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY / ".env.example"

LONG_RUNNING = ("postgres", "ollama", "rerank", "lightrag", "indexer")
WITH_HEALTHCHECK = ("postgres", "ollama", "rerank", "lightrag")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def env_example():
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _env(service):
    env = service.get("environment", {})
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return env


def test_all_services_defined(compose):
    assert set(compose["services"]) == {*LONG_RUNNING, "ollama-pull"}


def test_all_lightrag_storages_are_postgres(env_example, compose):
    expected = {
        "LIGHTRAG_KV_STORAGE": "PGKVStorage",
        "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage",
        "LIGHTRAG_GRAPH_STORAGE": "PGGraphStorage",
        "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
    }
    for key, value in expected.items():
        assert env_example[key] == value
        for name in ("lightrag", "indexer"):
            assert _env(compose["services"][name])[key] == f"${{{key}}}"


def test_summary_language_korean(env_example, compose):
    assert env_example["SUMMARY_LANGUAGE"] == "Korean"
    for name in ("lightrag", "indexer"):
        assert _env(compose["services"][name])["SUMMARY_LANGUAGE"] == "${SUMMARY_LANGUAGE}"


def test_model_names_are_env_references(compose):
    text = COMPOSE.read_text(encoding="utf-8").lower()
    for literal in ("qwen", "bge-m3", "bge-reranker", "kure", "exaone"):
        assert literal not in text
    for name in ("lightrag", "indexer"):
        env = _env(compose["services"][name])
        for key in ("LLM_MODEL", "EMBEDDING_MODEL", "RERANK_MODEL", "LLM_BINDING_HOST"):
            assert env[key] == f"${{{key}}}"
    assert "${RERANK_MODEL}" in str(compose["services"]["rerank"]["command"])


def test_env_example_models_are_licensed_and_local(env_example):
    assert env_example["LLM_MODEL"].startswith("qwen3")
    assert env_example["EMBEDDING_MODEL"].startswith("bge-m3")
    assert env_example["RERANK_MODEL"] == "BAAI/bge-reranker-v2-m3"
    joined = " ".join(env_example.values()).lower()
    assert "exaone" not in joined
    for key, value in env_example.items():
        if key.endswith("_HOST") and value.startswith("http"):
            host = re.match(r"https?://([^:/]+)", value).group(1)
            assert host in {"ollama", "rerank", "localhost", "host.docker.internal", "127.0.0.1"}


def test_chunk_size_is_one_env_value_for_server_and_indexer(env_example, compose):
    """R2-A2: AC10 의 토큰 한도(CHUNK_SIZE)를 .env 한 곳에서 정하고 서버 · 인덱서에 같이 준다."""
    assert env_example["CHUNK_SIZE"] == "1200"
    for name in ("lightrag", "indexer"):
        assert _env(compose["services"][name])["CHUNK_SIZE"] == "${CHUNK_SIZE}"


def test_postgres_password_is_required_without_default(env_example, compose):
    """S8: 비밀번호 기본값(change-me)으로 기동되지 않는다 — 비어 있으면 compose 가 거부한다."""
    assert env_example["POSTGRES_PASSWORD"] == ""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "change-me" not in text and "change-me" not in ENV_EXAMPLE.read_text(encoding="utf-8")
    for name in ("postgres", "lightrag", "indexer"):
        value = _env(compose["services"][name])["POSTGRES_PASSWORD"]
        assert value.startswith("${POSTGRES_PASSWORD:?"), name


def test_env_example_non_thinking_llm(env_example):
    assert env_example["OLLAMA_LLM_THINK"] == "false"


def test_ports_bound_to_localhost_only(compose):
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_services_have_healthchecks(compose):
    for name in WITH_HEALTHCHECK:
        assert "test" in compose["services"][name]["healthcheck"], name


def test_dependencies_wait_for_health(compose):
    indexer = compose["services"]["indexer"]["depends_on"]
    assert indexer["postgres"]["condition"] == "service_healthy"
    assert indexer["ollama-pull"]["condition"] == "service_completed_successfully"
    assert indexer["lightrag"]["condition"] == "service_healthy"
    lightrag = compose["services"]["lightrag"]["depends_on"]
    assert lightrag["postgres"]["condition"] == "service_healthy"


def test_ollama_pull_is_one_shot_and_uses_env_models(compose):
    pull = compose["services"]["ollama-pull"]
    assert pull.get("restart", "no") == "no"
    assert "OLLAMA_PULL_MODELS" in str(pull["command"])


def test_model_data_in_named_volumes(compose):
    assert {"pgdata", "ollama", "hf-cache"}.issubset(set(compose["volumes"]))


def test_postgres_init_creates_vector_and_age():
    sql = (DEPLOY / "postgres" / "init.sql").read_text(encoding="utf-8").lower()

    assert "create extension if not exists vector" in sql
    assert "create extension if not exists age" in sql


def test_postgres_image_pins_pg16_and_age():
    dockerfile = (DEPLOY / "postgres" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM pgvector/pgvector:pg16" in dockerfile
    assert re.search(r"AGE_REF=PG16/v\d+\.\d+\.\d+", dockerfile)


def test_llm_timeout_is_passed_to_lightrag_and_indexer(env_example, compose):
    """PPS-349 AC1: .env 의 LLM_TIMEOUT 이 컨테이너 env 로 전달돼야 워커 한도가 따라간다."""
    assert int(env_example["LLM_TIMEOUT"]) > 0
    for name in ("lightrag", "indexer"):
        assert _env(compose["services"][name])["LLM_TIMEOUT"] == "${LLM_TIMEOUT}", name


def test_num_ctx_is_passed_to_lightrag_and_indexer(env_example, compose):
    """PPS-349 AC3: 기본값 없는 참조라 .env.example 에 키가 없으면 빈 값이 컨테이너로 간다."""
    assert int(env_example["OLLAMA_LLM_NUM_CTX"]) > 0
    for name in ("lightrag", "indexer"):
        assert _env(compose["services"][name])["OLLAMA_LLM_NUM_CTX"] == "${OLLAMA_LLM_NUM_CTX}"


def test_indexer_has_stop_grace_period(compose):
    """AC6 의 정적 전제 — 유예 기간이 초 단위로 명시돼 있고 docker 기본값(10s)보다 길다.

    실제 SIGTERM 으로 137(SIGKILL) 없이 끝나는지는 이 테스트가 닫지 않는다(runbook §8 수동 검증).
    """
    grace = str(compose["services"]["indexer"]["stop_grace_period"])

    assert re.fullmatch(r"\d+s", grace)
    # 하한은 docker 기본 유예(10s)를 넘는다는 것뿐이다. 구체 값(현재 30s)은 runbook 이 '추정,
    # 실측으로 조정'이라 한 값이므로 여기서 고정하지 않는다.
    assert int(grace[:-1]) > 10


def test_app_dockerfile_runs_indexer_without_dev_deps():
    dockerfile = (DEPLOY / "app.Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev" in dockerfile
    assert "specgraph-indexer" in dockerfile
