"""환경변수 설정.

자체 변수는 ``SPECGRAPH_`` 접두어를 쓰고, LightRAG 서버와 공유하는 변수(``LLM_MODEL``,
``EMBEDDING_*``, ``RERANK_*``, ``SUMMARY_LANGUAGE``, ``POSTGRES_*``, ``LIGHTRAG_*_STORAGE``)는
LightRAG 규약 이름 그대로 읽는다. 그래서 ``.env`` 하나가 서버·인덱서·MCP 를 모두 구동한다.

모델명은 기본값이 없다(코드에 모델 리터럴을 두지 않는다 — AC2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from specgraph.errors import ConfigError

DEFAULT_INCLUDE_DIRS = ("prd", "ui-ux-spec", "tech-spec", "qa")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    # --- product-docs 원격 (SPECGRAPH_*) ---
    repo_url: str = Field(validation_alias="SPECGRAPH_REPO_URL", min_length=1)
    git_token: SecretStr | None = Field(default=None, validation_alias="SPECGRAPH_GIT_TOKEN")
    branch_glob: str = Field(default="draft/*", validation_alias="SPECGRAPH_BRANCH_GLOB")
    include_dirs: Annotated[tuple[str, ...], NoDecode] = Field(
        default=DEFAULT_INCLUDE_DIRS, validation_alias="SPECGRAPH_INCLUDE_DIRS"
    )
    poll_interval_seconds: int = Field(
        default=300, gt=0, validation_alias="SPECGRAPH_POLL_INTERVAL_SECONDS"
    )
    cache_dir: Path = Field(
        default=Path(".cache/product-docs"), validation_alias="SPECGRAPH_CACHE_DIR"
    )
    working_dir: Path = Field(
        default=Path(".cache/lightrag"), validation_alias="SPECGRAPH_WORKING_DIR"
    )
    git_timeout_seconds: float = Field(
        default=120, gt=0, validation_alias="SPECGRAPH_GIT_TIMEOUT_SECONDS"
    )
    log_format: Literal["kv", "json"] = Field(default="kv", validation_alias="SPECGRAPH_LOG_FORMAT")
    log_level: str = Field(default="INFO", validation_alias="SPECGRAPH_LOG_LEVEL")
    max_block_chars: int = Field(default=1000, gt=0, validation_alias="SPECGRAPH_MAX_BLOCK_CHARS")

    # --- 모델 · 엔드포인트 (LightRAG 규약 이름) ---
    llm_model: str = Field(validation_alias="LLM_MODEL", min_length=1)
    llm_binding_host: str = Field(
        default="http://localhost:11434", validation_alias="LLM_BINDING_HOST"
    )
    llm_timeout: int = Field(default=600, gt=0, validation_alias="LLM_TIMEOUT")
    llm_num_ctx: int = Field(default=16384, gt=0, validation_alias="OLLAMA_LLM_NUM_CTX")
    embedding_binding: Literal["ollama", "openai"] = Field(
        default="ollama", validation_alias="EMBEDDING_BINDING"
    )
    embedding_model: str = Field(validation_alias="EMBEDDING_MODEL", min_length=1)
    embedding_dim: int = Field(default=1024, gt=0, validation_alias="EMBEDDING_DIM")
    embedding_binding_host: str = Field(
        default="http://localhost:11434", validation_alias="EMBEDDING_BINDING_HOST"
    )
    rerank_binding: Literal["cohere"] = Field(default="cohere", validation_alias="RERANK_BINDING")
    rerank_model: str | None = Field(default=None, validation_alias="RERANK_MODEL")
    rerank_binding_host: str | None = Field(default=None, validation_alias="RERANK_BINDING_HOST")
    summary_language: str = Field(default="Korean", validation_alias="SUMMARY_LANGUAGE")
    # LightRAG 청크 토큰 한도. 블록 재분할(AC10)의 기준이며 LightRAG 서버와 같은 변수를 쓴다.
    chunk_token_size: int = Field(default=1200, gt=0, validation_alias="CHUNK_SIZE")

    # --- PostgreSQL ---
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    postgres_user: str = Field(default="specgraph", validation_alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(
        default=SecretStr(""), validation_alias="POSTGRES_PASSWORD"
    )
    postgres_database: str = Field(default="specgraph", validation_alias="POSTGRES_DATABASE")

    # --- LightRAG 스토리지 (AC1: 전부 PostgreSQL) ---
    kv_storage: str = Field(default="PGKVStorage", validation_alias="LIGHTRAG_KV_STORAGE")
    vector_storage: str = Field(
        default="PGVectorStorage", validation_alias="LIGHTRAG_VECTOR_STORAGE"
    )
    graph_storage: str = Field(default="PGGraphStorage", validation_alias="LIGHTRAG_GRAPH_STORAGE")
    doc_status_storage: str = Field(
        default="PGDocStatusStorage", validation_alias="LIGHTRAG_DOC_STATUS_STORAGE"
    )

    @field_validator("include_dirs", mode="before")
    @classmethod
    def _split_dirs(cls, value: Any) -> Any:
        if isinstance(value, str):
            return tuple(part.strip().strip("/") for part in value.split(",") if part.strip())
        return value

    def lightrag_addon_params(self) -> dict[str, Any]:
        return {"language": self.summary_language}

    def postgres_dsn(self) -> str:
        """사용자 · 비밀번호 · DB 이름은 URL 인코딩한다(``@ : / # ?`` 같은 문자가 있어도 안전)."""
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")
        database = quote(self.postgres_database, safe="")
        return (
            f"postgresql://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{database}"
        )


def load_settings() -> Settings:
    """환경변수에서 Settings 를 읽는다. 누락·형식 오류는 ConfigError 로 바꾼다."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = ", ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"환경변수 설정 오류 — {problems}") from exc
