"""FastMCP 서버 — 다섯 도구 (AC18 · AC19 · AC17, PPS-350 AC11 · AC14).

``query(question, mode, branch, hl_keywords, ll_keywords)`` · ``get_chapter(doc, section)`` ·
``find_screen(id)`` · ``find_policy(id)`` · ``list_docs()`` (도구명 · 인자명은 티켓 고정).
``query`` 는 답변을 만들지 않고 근거(청크 · 엔티티 · 관계, 각각 doc_id · commit_sha)만 돌려준다 —
키워드는 호출자(Claude Code)가 뽑아 넘기고, 이 서버는 LLM 을 쓰지 않는다.
의존성(PostgreSQL manifest · LightRAG)은 첫 도구 호출 때 한 번 만들고, 서버가 끝날 때(lifespan)
닫는다. 모든 도메인 예외(``SpecGraphError`` — 초기화 실패 포함)는 도구 오류 응답으로 바꾼다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from specgraph.errors import SpecGraphError
from specgraph.mcp_server.lookup import LookupService
from specgraph.mcp_server.query_service import QueryService

TOOL_NAMES = ("query", "get_chapter", "find_screen", "find_policy", "list_docs")

QueryMode = Literal["local", "global", "hybrid", "naive", "mix"]

INSTRUCTIONS = (
    "product-docs 의 draft/* 브랜치 기획 문서(PRD · UI/UX spec · tech-spec · QA) 지식 그래프. "
    "doc_id 형식은 <branch>:<path>#<§번호>. 모든 응답에 doc_id 와 commit_sha 출처가 있다. "
    "query 는 답변이 아니라 근거(청크 · 엔티티 · 관계)만 돌려준다 — 호출자가 질문에서 "
    "hl_keywords(주제 · 관계 수준)와 ll_keywords(구체 엔티티 · 화면/정책 ID)를 뽑아 넘기고, "
    "돌아온 근거로 답을 만들며 doc_id 를 인용한다."
)


@dataclass
class Services:
    query: QueryService
    lookup: LookupService
    # 서버 종료 때 부를 정리 함수(asyncpg 풀 · LightRAG 스토리지 닫기).
    close: Callable[[], Awaitable[None]] | None = None


ServicesFactory = Callable[[], Awaitable[Services]]


class _Lazy:
    def __init__(self, factory: ServicesFactory) -> None:
        self._factory = factory
        self._services: Services | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Services:
        async with self._lock:
            if self._services is None:
                self._services = await self._factory()
            return self._services

    async def close(self) -> None:
        async with self._lock:
            services, self._services = self._services, None
        if services is not None and services.close is not None:
            await services.close()


async def _call(lazy: _Lazy, action: Callable[[Services], Awaitable[Any]]) -> Any:
    """의존성 준비와 도구 실행을 함께 감싸, 도메인 예외를 도구 오류 응답으로 바꾼다."""
    try:
        return await action(await lazy.get())
    except SpecGraphError as exc:
        raise ToolError(str(exc)) from exc


def build_server(services: ServicesFactory) -> FastMCP:
    lazy = _Lazy(services)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await lazy.close()

    mcp = FastMCP("specgraph", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool
    async def query(
        question: Annotated[str, Field(description="한국어 질문")],
        mode: Annotated[QueryMode, Field(description="LightRAG 검색 모드")] = "mix",
        branch: Annotated[
            str | None,
            Field(description="브랜치 한정(예: draft/<프로젝트>-<문서명>). 없으면 전체 브랜치"),
        ] = None,
        hl_keywords: Annotated[
            list[str] | None,
            Field(
                description=(
                    "관계(global/hybrid/mix) 검색용 상위 키워드 — 주제 · 개념 수준"
                    "(예: '정산 정책', '지급 일정')"
                )
            ),
        ] = None,
        ll_keywords: Annotated[
            list[str] | None,
            Field(
                description=(
                    "엔티티(local/hybrid/mix) 검색용 하위 키워드 — 구체 엔티티 · 화면/정책 ID"
                    "(예: 'P-14.3', 'ADM-03'). 둘 다 비면 질문 전체를 하위 키워드로 쓴다"
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """지식 그래프에서 근거를 검색한다(LLM 호출 없음).

        청크 본문 · 엔티티 · 관계를 각각 doc_id · commit_sha 출처와 함께 반환하고, 근거가 없으면
        빈 목록과 no_evidence=true 를 돌려준다. 답변은 호출자가 이 근거로 만든다.
        """
        result = await _call(
            lazy, lambda s: s.query.query(question, mode, branch, hl_keywords, ll_keywords)
        )
        return result.to_dict()

    @mcp.tool
    async def get_chapter(
        doc: Annotated[
            str,
            Field(description="문서(<branch>:<path>, 브랜치명, 또는 짧은 이름 예: admin-prd)"),
        ],
        section: Annotated[str, Field(description="챕터 번호(예: 5, §5, 5.2 → 5, preamble)")],
    ) -> dict[str, Any]:
        """챕터 원문을 doc_id · commit_sha 와 함께 반환한다."""
        return await _call(lazy, lambda s: s.lookup.get_chapter(doc, section))

    @mcp.tool
    async def find_screen(
        id: Annotated[str, Field(description="화면 ID (예: ADM-03, PTN-P-04)")],
    ) -> dict[str, Any]:
        """화면 ID 가 정의된 챕터의 원문과 doc_id · commit_sha 를 반환한다."""
        return await _call(lazy, lambda s: s.lookup.find_screen(id))

    @mcp.tool
    async def find_policy(
        id: Annotated[str, Field(description="정책 ID (예: P-14.3, U-2)")],
    ) -> dict[str, Any]:
        """정책 ID 가 정의된 챕터의 원문과 doc_id · commit_sha 를 반환한다."""
        return await _call(lazy, lambda s: s.lookup.find_policy(id))

    @mcp.tool
    async def list_docs() -> dict[str, Any]:
        """인덱싱된 브랜치 · 문서 · 챕터 목록(doc_id · commit_sha 포함)."""
        return await _call(lazy, lambda s: s.lookup.list_docs())

    return mcp
