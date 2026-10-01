import pytest
from fastmcp import Client

from fakes import FakeLightRAG, InMemoryManifest
from specgraph.lightrag_store import Retrieval, RetrievedChunk
from specgraph.manifest import ChapterRecord
from specgraph.mcp_server.lookup import LookupService
from specgraph.mcp_server.query_service import QueryService
from specgraph.mcp_server.server import TOOL_NAMES, Services, build_server

ADM3 = "draft/settlr-admin-prd:prd/settlr-admin-prd.md#3"


def _record():
    return ChapterRecord(
        doc_id=ADM3,
        branch="draft/settlr-admin-prd",
        path="prd/settlr-admin-prd.md",
        section="3",
        title="3. ADM-03 정산 내역",
        content="## 3. ADM-03 정산 내역\n운영자 화면",
        content_hash="h",
        commit_sha="sha-a",
        content_commit_sha="sha-a",
        screens={"ADM-03": 0},
        policies={},
    )


class Llm:
    async def __call__(self, prompt, system_prompt=None, **_):
        return "답변"


@pytest.fixture
def server():
    manifest = InMemoryManifest(
        records={ADM3: _record()}, heads={"draft/settlr-admin-prd": "sha-a"}
    )
    rag = FakeLightRAG(retrieval=Retrieval(chunks=[RetrievedChunk(ADM3, "운영자 화면")]))

    async def services() -> Services:
        return Services(query=QueryService(rag, manifest, Llm()), lookup=LookupService(manifest))

    return build_server(services)


async def test_server_exposes_exactly_five_tools(server):
    async with Client(server) as client:
        tools = await client.list_tools()

    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    assert sorted(TOOL_NAMES) == ["find_policy", "find_screen", "get_chapter", "list_docs", "query"]


async def test_tool_signatures_match_ticket(server):
    async with Client(server) as client:
        tools = {t.name: t.input_schema for t in await client.list_tools()}

    assert list(tools["query"]["properties"]) == ["question", "mode", "branch"]
    assert tools["query"]["required"] == ["question"]
    assert set(tools["query"]["properties"]["mode"]["enum"]) == {
        "local",
        "global",
        "hybrid",
        "naive",
        "mix",
    }
    assert list(tools["get_chapter"]["properties"]) == ["doc", "section"]
    assert list(tools["find_screen"]["properties"]) == ["id"]
    assert list(tools["find_policy"]["properties"]) == ["id"]
    assert tools["list_docs"].get("properties", {}) == {}


async def test_find_screen_tool_end_to_end_with_fakes(server):
    async with Client(server) as client:
        result = await client.call_tool("find_screen", {"id": "ADM-03"})

    data = result.structured_content
    assert data["doc_id"] == ADM3
    assert data["commit_sha"] == "sha-a"
    assert data["content"].startswith("## 3. ADM-03")


async def test_query_tool_returns_sources_with_commit_sha(server):
    async with Client(server) as client:
        result = await client.call_tool("query", {"question": "ADM-03 은?"})

    data = result.structured_content
    assert data["answer"] == "답변"
    assert data["sources"] == [{"doc_id": ADM3, "commit_sha": "sha-a"}]


async def test_lookup_tools_include_doc_id_and_commit_sha(server):
    async with Client(server) as client:
        chapter = await client.call_tool("get_chapter", {"doc": "admin-prd", "section": "3"})
        docs = await client.call_tool("list_docs", {})

    assert chapter.structured_content["doc_id"] == ADM3
    assert chapter.structured_content["commit_sha"] == "sha-a"
    chapters = docs.structured_content["branches"][0]["documents"][0]["chapters"]
    assert chapters == [
        {"section": "3", "title": "3. ADM-03 정산 내역", "doc_id": ADM3, "commit_sha": "sha-a"}
    ]


async def test_not_found_becomes_tool_error(server):
    async with Client(server) as client:
        result = await client.call_tool("find_screen", {"id": "ZZZ-99"}, raise_on_error=False)

    assert result.is_error
    assert "ZZZ-99" in result.content[0].text


async def test_invalid_mode_becomes_tool_error(server):
    async with Client(server) as client:
        result = await client.call_tool(
            "query", {"question": "q", "mode": "bypass"}, raise_on_error=False
        )

    assert result.is_error


async def test_service_initialization_failure_becomes_clear_tool_error():
    """A2: 첫 호출의 의존성 초기화 실패(도메인 예외)도 도구 오류 응답으로 바꾼다."""
    from fastmcp.exceptions import ToolError

    from specgraph.errors import ConfigError
    from specgraph.mcp_server.server import _call

    async def broken() -> Services:
        raise ConfigError("환경변수 설정 오류 — LLM_MODEL: Field required")

    lazy_server = build_server(broken)
    async with Client(lazy_server) as client:
        result = await client.call_tool("list_docs", {}, raise_on_error=False)

    assert result.is_error
    assert "LLM_MODEL" in result.content[0].text

    from specgraph.mcp_server.server import _Lazy

    with pytest.raises(ToolError, match="LLM_MODEL"):
        await _call(_Lazy(broken), lambda s: s.lookup.list_docs())


async def test_any_domain_error_from_a_tool_becomes_tool_error():
    from fastmcp.exceptions import ToolError

    from specgraph.errors import IndexingError
    from specgraph.mcp_server.server import _call, _Lazy

    class Failing:
        async def list_docs(self):
            raise IndexingError("LightRAG 조회 실패")

    async def services() -> Services:
        return Services(query=None, lookup=Failing())  # type: ignore[arg-type]

    with pytest.raises(ToolError, match="LightRAG 조회 실패"):
        await _call(_Lazy(services), lambda s: s.lookup.list_docs())


async def test_services_are_closed_when_server_shuts_down():
    """S5: 서버 종료(lifespan 끝) 때 만든 의존성(asyncpg 풀 · LightRAG)을 닫는다."""
    closed = []
    manifest = InMemoryManifest(records={ADM3: _record()})

    async def close() -> None:
        closed.append(True)

    async def services() -> Services:
        return Services(
            query=QueryService(FakeLightRAG(), manifest, Llm()),
            lookup=LookupService(manifest),
            close=close,
        )

    async with Client(build_server(services)) as client:
        await client.call_tool("list_docs", {})
        assert closed == []

    assert closed == [True]


async def test_real_services_closes_manifest_when_lightrag_init_fails(settings_env, monkeypatch):
    """S5: 초기화가 중간에 실패하면 이미 만든 asyncpg 풀을 닫는다."""
    from specgraph.lightrag_store import LightRagStore
    from specgraph.manifest import PostgresManifest
    from specgraph.mcp_server import __main__ as entry

    class FakeManifest:
        closed = False

        async def close(self):
            FakeManifest.closed = True

    async def create_manifest(dsn, schema="specgraph"):
        return FakeManifest()

    async def create_store(settings, counter, **_):
        raise RuntimeError("LightRAG 초기화 실패")

    monkeypatch.setattr(PostgresManifest, "create", staticmethod(create_manifest))
    monkeypatch.setattr(LightRagStore, "create", staticmethod(create_store))

    with pytest.raises(RuntimeError):
        await entry.real_services()

    assert FakeManifest.closed


def test_stdio_entrypoint_is_registered_as_script():
    import tomllib
    from pathlib import Path

    from specgraph.mcp_server import __main__ as entry

    pyproject = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["scripts"]["specgraph-mcp"] == "specgraph.mcp_server.__main__:main"
    assert callable(entry.main)
    assert callable(entry.real_services)
