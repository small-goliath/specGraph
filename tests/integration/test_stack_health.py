"""AC1 — compose 스택 기동 상태 (M1 의 자동화 부분). 스택이 없으면 STACK_HINT 로 실패한다."""

import os

import asyncpg
import httpx
import pytest

from integration.conftest import require_stack

pytestmark = pytest.mark.integration

PG_STORAGES = ("PGKVStorage", "PGVectorStorage", "PGGraphStorage", "PGDocStatusStorage")


async def test_postgres_has_vector_and_age_extensions(live_settings):
    conn = await require_stack(asyncpg.connect(live_settings.postgres_dsn()), "PostgreSQL")
    try:
        rows = await conn.fetch("SELECT extname FROM pg_extension")
    finally:
        await conn.close()

    assert {"vector", "age"}.issubset({r["extname"] for r in rows})


async def test_lightrag_server_health_reports_postgres_storages():
    port = os.environ.get("LIGHTRAG_HOST_PORT", "9621")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await require_stack(
            client.get(f"http://127.0.0.1:{port}/health"), "LightRAG 서버"
        )

    assert response.status_code == 200
    body = response.text
    for storage in PG_STORAGES:
        assert storage in body
    assert "Korean" in body


async def test_ollama_serves_llm_and_embedding_models(live_settings):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await require_stack(
            client.get(f"{live_settings.llm_binding_host}/api/tags"), "Ollama"
        )

    names = {m["name"] for m in response.json()["models"]}
    assert any(n.split(":")[0] == live_settings.llm_model.split(":")[0] for n in names)
    if live_settings.embedding_binding == "ollama":
        assert any(n.split(":")[0] == live_settings.embedding_model.split(":")[0] for n in names)


async def test_reranker_is_healthy_and_ranks(live_settings):
    assert live_settings.rerank_binding_host, "RERANK_BINDING_HOST 가 필요하다"
    base = live_settings.rerank_binding_host.rsplit("/rerank", 1)[0]
    async with httpx.AsyncClient(timeout=60) as client:
        health = await require_stack(client.get(f"{base}/health"), "리랭커(Infinity)")
        ranked = await client.post(
            live_settings.rerank_binding_host,
            json={
                "model": live_settings.rerank_model,
                "query": "정산 주기",
                "documents": ["정산 주기는 D+2 이다", "로그인 화면"],
            },
        )

    assert health.status_code == 200
    results = ranked.json()["results"]
    assert results[0]["index"] == 0
