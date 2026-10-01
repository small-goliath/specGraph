"""통합 테스트 픽스처.

docker compose 스택(PostgreSQL · Ollama · Infinity · LightRAG)이 떠 있어야 한다.

실행:
    set -a; source deploy/.env; set +a
    export POSTGRES_HOST=localhost LLM_BINDING_HOST=http://localhost:11434 \\
           EMBEDDING_BINDING_HOST=http://localhost:11434 \\
           RERANK_BINDING_HOST=http://localhost:7997/rerank
    uv run pytest -m integration

스택이 없으면 skip 이 아니라 원인을 적은 실패로 끝난다(계획 T18). 외부 서비스 연결은
``require_stack`` 으로 감싸 연결 실패를 STACK_HINT 와 함께 알린다.
매 테스트는 별도 manifest 스키마와 LightRAG workspace 를 써서 실제 인덱스를 건드리지 않고,
끝나면 어댑터의 공개 정리 메서드(``drop_schema`` · ``drop_workspace``)로 지운다. 정리 실패는
삼키지 않고 테스트 오류로 드러낸다.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable

import pytest

from specgraph.errors import ConfigError
from specgraph.lightrag_store import LightRagStore
from specgraph.llm import LlmCallCounter
from specgraph.manifest import PostgresManifest
from specgraph.settings import Settings, load_settings

STACK_HINT = (
    "docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d 로 스택을 띄우고 "
    "deploy/.env 를 export 한 뒤(POSTGRES_HOST=localhost 등) 다시 실행하라"
)


async def require_stack[T](awaitable: Awaitable[T], what: str) -> T:
    """외부 서비스 호출. 연결 실패면 원시 예외 대신 STACK_HINT 를 담은 실패로 끝낸다."""
    try:
        return await awaitable
    except (OSError, ConnectionError) as exc:
        pytest.fail(f"{what} 에 연결하지 못했다({type(exc).__name__}: {exc}). {STACK_HINT}")
    except Exception as exc:  # noqa: BLE001 — httpx · asyncpg 연결 예외 계층이 서로 다르다
        if "connect" in type(exc).__name__.lower():
            pytest.fail(f"{what} 에 연결하지 못했다({type(exc).__name__}: {exc}). {STACK_HINT}")
        raise


@pytest.fixture
def live_settings() -> Settings:
    """실제 셸 환경변수를 그대로 읽는다(루트 conftest 의 clean_env 를 쓰지 않는다)."""
    try:
        return load_settings()
    except ConfigError as exc:
        pytest.fail(f"통합 테스트 환경변수가 없다: {exc}. {STACK_HINT}")


@pytest.fixture
def unique() -> str:
    return f"itest_{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def pg_manifest(live_settings: Settings, unique: str) -> AsyncIterator[PostgresManifest]:
    try:
        manifest = await PostgresManifest.create(live_settings.postgres_dsn(), schema=unique)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"PostgreSQL 연결 실패({exc}). {STACK_HINT}")
    try:
        yield manifest
    finally:
        try:
            await manifest.drop_schema()
        finally:
            await manifest.close()


@pytest.fixture
def counter() -> LlmCallCounter:
    return LlmCallCounter()


@pytest.fixture
async def live_store(
    live_settings: Settings, unique: str, counter: LlmCallCounter, tmp_path
) -> AsyncIterator[LightRagStore]:
    try:
        store = await LightRagStore.create(
            live_settings, counter, workspace=unique, working_dir=str(tmp_path / "rag")
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"LightRAG(PostgreSQL) 초기화 실패({exc}). {STACK_HINT}")
    try:
        yield store
    finally:
        try:
            await store.drop_workspace()
        finally:
            await store.close()
