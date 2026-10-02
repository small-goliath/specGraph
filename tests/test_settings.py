from pathlib import Path

import pytest

from specgraph.errors import ConfigError, SpecGraphError
from specgraph.settings import load_settings


def test_env_overrides_model_names_and_endpoints(settings_env):
    settings_env.setenv("LLM_MODEL", "qwen-like:30b")
    settings_env.setenv("LLM_BINDING_HOST", "http://llm.internal:11434")
    settings_env.setenv("EMBEDDING_MODEL", "embed-x")
    settings_env.setenv("EMBEDDING_DIM", "768")
    settings_env.setenv("EMBEDDING_BINDING_HOST", "http://embed.internal:7997")
    settings_env.setenv("RERANK_MODEL", "rerank-x")
    settings_env.setenv("RERANK_BINDING_HOST", "http://rerank.internal:7997")

    s = load_settings()

    assert s.llm_model == "qwen-like:30b"
    assert s.llm_binding_host == "http://llm.internal:11434"
    assert s.embedding_model == "embed-x"
    assert s.embedding_dim == 768
    assert s.embedding_binding_host == "http://embed.internal:7997"
    assert s.rerank_model == "rerank-x"
    assert s.rerank_binding_host == "http://rerank.internal:7997"


def test_summary_language_defaults_to_korean(settings_env):
    s = load_settings()

    assert s.summary_language == "Korean"
    assert s.lightrag_addon_params()["language"] == "Korean"


def test_summary_language_from_env(settings_env):
    settings_env.setenv("SUMMARY_LANGUAGE", "English")

    assert load_settings().lightrag_addon_params()["language"] == "English"


def test_chunk_size_default_and_env(settings_env):
    assert load_settings().chunk_token_size == 1200

    settings_env.setenv("CHUNK_SIZE", "800")

    assert load_settings().chunk_token_size == 800


def test_postgres_dsn_url_encodes_credentials(settings_env):
    """S6: 특수문자가 있는 사용자 · 비밀번호도 DSN 에서 깨지지 않는다."""
    from urllib.parse import unquote, urlsplit

    settings_env.setenv("POSTGRES_USER", "spec user")
    settings_env.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1?")
    settings_env.setenv("POSTGRES_HOST", "db.local")
    settings_env.setenv("POSTGRES_PORT", "6543")

    parts = urlsplit(load_settings().postgres_dsn())

    assert unquote(parts.username) == "spec user"
    assert unquote(parts.password) == "p@ss:w/rd#1?"
    assert parts.hostname == "db.local" and parts.port == 6543
    assert parts.path == "/specgraph"


def test_poll_interval_from_env(settings_env):
    settings_env.setenv("SPECGRAPH_POLL_INTERVAL_SECONDS", "42")

    assert load_settings().poll_interval_seconds == 42


def test_poll_interval_must_be_positive(settings_env):
    settings_env.setenv("SPECGRAPH_POLL_INTERVAL_SECONDS", "0")

    with pytest.raises(ConfigError):
        load_settings()


def test_git_timeout_default_and_env(settings_env):
    assert load_settings().git_timeout_seconds == 120

    settings_env.setenv("SPECGRAPH_GIT_TIMEOUT_SECONDS", "15")

    assert load_settings().git_timeout_seconds == 15


def test_git_timeout_must_be_positive(settings_env):
    settings_env.setenv("SPECGRAPH_GIT_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigError):
        load_settings()


def test_missing_repo_url_raises_config_error(settings_env):
    settings_env.delenv("SPECGRAPH_REPO_URL")

    with pytest.raises(ConfigError) as exc:
        load_settings()

    assert isinstance(exc.value, SpecGraphError)
    assert "SPECGRAPH_REPO_URL" in str(exc.value)


def test_missing_llm_model_raises_config_error(settings_env):
    settings_env.delenv("LLM_MODEL")

    with pytest.raises(ConfigError):
        load_settings()


def test_defaults_branch_glob_and_include_dirs(settings_env):
    s = load_settings()

    assert s.branch_glob == "draft/*"
    assert s.include_dirs == ("prd", "ui-ux-spec", "tech-spec", "qa")


def test_include_dirs_comma_separated(settings_env):
    settings_env.setenv("SPECGRAPH_INCLUDE_DIRS", " prd , qa ,")

    assert load_settings().include_dirs == ("prd", "qa")


def test_storages_default_to_postgres(settings_env):
    s = load_settings()

    assert s.kv_storage == "PGKVStorage"
    assert s.vector_storage == "PGVectorStorage"
    assert s.graph_storage == "PGGraphStorage"
    assert s.doc_status_storage == "PGDocStatusStorage"


def test_git_token_is_secret(settings_env):
    settings_env.setenv("SPECGRAPH_GIT_TOKEN", "ghp_secretvalue")

    s = load_settings()

    assert s.git_token is not None
    assert s.git_token.get_secret_value() == "ghp_secretvalue"
    assert "ghp_secretvalue" not in repr(s)


def test_bindings_default_and_env(settings_env):
    s = load_settings()
    assert s.embedding_binding == "ollama"
    assert s.rerank_binding == "cohere"

    settings_env.setenv("EMBEDDING_BINDING", "openai")
    assert load_settings().embedding_binding == "openai"


def test_unknown_binding_rejected(settings_env):
    settings_env.setenv("EMBEDDING_BINDING", "bedrock")

    with pytest.raises(ConfigError):
        load_settings()


def test_llm_num_ctx_defaults_to_16384_and_reads_env(settings_env):
    assert load_settings().llm_num_ctx == 16384

    settings_env.setenv("OLLAMA_LLM_NUM_CTX", "8192")

    assert load_settings().llm_num_ctx == 8192


def test_llm_num_ctx_rejects_non_positive(settings_env):
    settings_env.setenv("OLLAMA_LLM_NUM_CTX", "0")

    with pytest.raises(ConfigError):
        load_settings()


def test_llm_timeout_default_and_env_override(settings_env):
    assert load_settings().llm_timeout == 600

    settings_env.setenv("LLM_TIMEOUT", "1800")

    assert load_settings().llm_timeout == 1800


def test_paths_are_paths(settings_env, tmp_path):
    settings_env.setenv("SPECGRAPH_CACHE_DIR", str(tmp_path / "c"))

    s = load_settings()

    assert s.cache_dir == Path(tmp_path / "c")
    assert isinstance(s.working_dir, Path)
