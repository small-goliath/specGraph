"""MCP 서버 stdio 진입점 — ``specgraph-mcp`` / ``python -m specgraph.mcp_server``.

stdout 은 MCP 프로토콜 전용이다. 로그는 stderr 로만 나간다(``configure_logging``).
LLM 은 쓰지 않는다(PPS-350 AC17): ``LLM_MODEL`` · ``LLM_BINDING_HOST`` 없이 뜨고, LightRAG
초기화에는 호출되면 ``LlmError`` 를 내는 비활성 스텁(``make_disabled_llm_func``)을 넘긴다(D8).
"""

from __future__ import annotations

import sys

from specgraph.mcp_server.server import Services, build_server


async def real_services() -> Services:
    """환경변수 설정으로 PostgreSQL manifest 와 LightRAG 를 연결한다(첫 도구 호출 때 1회).

    LightRAG 초기화가 실패하면 이미 만든 asyncpg 풀을 닫고 예외를 그대로 올린다. 만든 의존성은
    서버 종료 때(``Services.close``) 닫힌다.
    """
    from specgraph.lightrag_store import LightRagStore
    from specgraph.llm import LlmCallCounter, make_disabled_llm_func
    from specgraph.manifest import PostgresManifest
    from specgraph.mcp_server.lookup import LookupService
    from specgraph.mcp_server.query_service import QueryService
    from specgraph.settings import load_settings

    settings = load_settings()
    counter = LlmCallCounter()
    manifest = await PostgresManifest.create(settings.postgres_dsn())
    try:
        store = await LightRagStore.create(
            settings,
            counter,
            llm_model_func=make_disabled_llm_func(counter),
            llm_model_name="disabled",
        )
    except BaseException:
        await manifest.close()
        raise

    async def close() -> None:
        try:
            await store.close()
        finally:
            await manifest.close()

    return Services(
        query=QueryService(store, manifest),
        lookup=LookupService(manifest),
        close=close,
    )


def main() -> int:
    from specgraph.errors import SpecGraphError
    from specgraph.log import configure_logging
    from specgraph.settings import load_settings

    try:
        settings = load_settings()
    except SpecGraphError as exc:
        print(f"specgraph-mcp: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_format, settings.log_level)
    build_server(real_services).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
