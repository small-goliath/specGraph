"""manifest — 챕터별 인덱싱 상태(내용 해시 · 커밋 SHA · 추출 ID · 소유 KG 키).

포트(``ManifestPort``)와 PostgreSQL 구현(``PostgresManifest``, 스키마 ``specgraph``)을 둔다.
doc_id 당 1행(브랜치 HEAD 최신본)만 유지한다 — 버전 이력은 보관하지 않는다(범위 밖).

커밋 SHA (결정 D6)
    ``commit_sha``          내용 해시가 확인된 최신 브랜치 HEAD(변경 없는 챕터도 갱신, LLM 0회)
    ``content_commit_sha``  그 내용이 LightRAG 에 삽입된 시점의 HEAD
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from specgraph.kg_model import KgKeys

INDEXER_LOCK_KEY = "specgraph-indexer"
DEFAULT_SCHEMA = "specgraph"
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_DDL_TEMPLATE = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.branch_heads (
    branch      TEXT PRIMARY KEY,
    head_sha    TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {s}.chapters (
    doc_id              TEXT PRIMARY KEY,
    branch              TEXT NOT NULL,
    path                TEXT NOT NULL,
    section             TEXT NOT NULL,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    commit_sha          TEXT NOT NULL,
    content_commit_sha  TEXT NOT NULL,
    screens             JSONB NOT NULL DEFAULT '{}'::jsonb,
    policies            JSONB NOT NULL DEFAULT '{}'::jsonb,
    kg_keys             JSONB,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chapters_branch_idx ON {s}.chapters (branch);
CREATE INDEX IF NOT EXISTS chapters_screens_idx ON {s}.chapters USING GIN (screens);
CREATE INDEX IF NOT EXISTS chapters_policies_idx ON {s}.chapters USING GIN (policies);
"""


def validate_schema(schema: str) -> str:
    """스키마 이름은 SQL 에 직접 들어가므로 소문자 식별자만 허용한다."""
    if not _SCHEMA_NAME.match(schema or ""):
        raise ValueError(f"스키마 이름이 올바르지 않다: {schema!r}")
    return schema


def render_ddl(schema: str = DEFAULT_SCHEMA) -> str:
    return _DDL_TEMPLATE.replace("{s}", validate_schema(schema))


@dataclass(frozen=True)
class ChapterRecord:
    doc_id: str
    branch: str
    path: str
    section: str
    title: str
    content: str
    content_hash: str
    commit_sha: str
    content_commit_sha: str
    screens: dict[str, int] = field(default_factory=dict)
    policies: dict[str, int] = field(default_factory=dict)
    kg_keys: KgKeys | None = None

    @property
    def document(self) -> str:
        return f"{self.branch}:{self.path}"

    def with_commit(self, commit_sha: str) -> ChapterRecord:
        return replace(self, commit_sha=commit_sha)


class ManifestPort(Protocol):
    async def branch_heads(self) -> dict[str, str]: ...

    async def chapter_branches(self) -> set[str]:
        """챕터가 하나라도 남아 있는 브랜치(HEAD 기록 유무와 무관)."""
        ...

    async def set_branch_head(self, branch: str, sha: str) -> None: ...

    async def delete_branch_head(self, branch: str) -> None: ...

    async def chapters_for_branch(self, branch: str) -> dict[str, ChapterRecord]: ...

    async def upsert_chapter(self, record: ChapterRecord) -> None: ...

    async def touch_chapters(self, doc_ids: list[str], commit_sha: str) -> None: ...

    async def delete_chapter(self, doc_id: str) -> None: ...

    async def get_chapter(self, doc_id: str) -> ChapterRecord | None: ...

    async def all_chapters(self) -> list[ChapterRecord]: ...

    async def chapters_with_screen(self, screen_id: str) -> list[ChapterRecord]: ...

    async def chapters_with_policy(self, policy_id: str) -> list[ChapterRecord]: ...

    async def referenced_names(self, names: list[str]) -> set[str]: ...


def _record_from_row(row: Any) -> ChapterRecord:
    def _json(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    kg_raw = row["kg_keys"]
    return ChapterRecord(
        doc_id=row["doc_id"],
        branch=row["branch"],
        path=row["path"],
        section=row["section"],
        title=row["title"],
        content=row["content"],
        content_hash=row["content_hash"],
        commit_sha=row["commit_sha"],
        content_commit_sha=row["content_commit_sha"],
        screens=dict(_json(row["screens"]) or {}),
        policies=dict(_json(row["policies"]) or {}),
        kg_keys=KgKeys.from_json(kg_raw if isinstance(kg_raw, str) else json.dumps(kg_raw))
        if kg_raw
        else None,
    )


def _select(schema: str) -> str:
    return (
        "SELECT doc_id, branch, path, section, title, content, content_hash, commit_sha, "
        "content_commit_sha, screens::text AS screens, policies::text AS policies, "
        f"kg_keys::text AS kg_keys FROM {schema}.chapters"
    )


class PostgresManifest:
    """asyncpg 풀 기반 구현. ``create()`` 가 스키마를 만든다(CREATE … IF NOT EXISTS)."""

    def __init__(self, pool: Any, schema: str = DEFAULT_SCHEMA) -> None:
        self._pool = pool
        self._s = validate_schema(schema)
        self._select = _select(self._s)
        self._lock_conn: Any | None = None

    @classmethod
    async def create(cls, dsn: str, schema: str = DEFAULT_SCHEMA) -> PostgresManifest:
        import asyncpg

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        async with pool.acquire() as conn:
            await conn.execute(render_ddl(schema))
        return cls(pool, schema)

    async def drop_schema(self) -> None:
        """이 manifest 의 스키마를 통째로 지운다(통합 테스트 정리용 — 운영에서 쓰지 않는다)."""
        await self._pool.execute(f"DROP SCHEMA IF EXISTS {self._s} CASCADE")

    async def close(self) -> None:
        if self._lock_conn is not None:
            await self._pool.release(self._lock_conn)
            self._lock_conn = None
        await self._pool.close()

    async def try_acquire_indexer_lock(self) -> bool:
        """중복 인덱서 데몬 방지용 세션 advisory lock."""
        if self._lock_conn is None:
            self._lock_conn = await self._pool.acquire()
        return bool(
            await self._lock_conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1))", f"{INDEXER_LOCK_KEY}:{self._s}"
            )
        )

    async def branch_heads(self) -> dict[str, str]:
        rows = await self._pool.fetch(f"SELECT branch, head_sha FROM {self._s}.branch_heads")
        return {r["branch"]: r["head_sha"] for r in rows}

    async def chapter_branches(self) -> set[str]:
        rows = await self._pool.fetch(f"SELECT DISTINCT branch FROM {self._s}.chapters")
        return {r["branch"] for r in rows}

    async def set_branch_head(self, branch: str, sha: str) -> None:
        await self._pool.execute(
            f"INSERT INTO {self._s}.branch_heads (branch, head_sha) VALUES ($1, $2) "
            "ON CONFLICT (branch) DO UPDATE SET head_sha = EXCLUDED.head_sha, updated_at = now()",
            branch,
            sha,
        )

    async def delete_branch_head(self, branch: str) -> None:
        await self._pool.execute(f"DELETE FROM {self._s}.branch_heads WHERE branch = $1", branch)

    async def chapters_for_branch(self, branch: str) -> dict[str, ChapterRecord]:
        rows = await self._pool.fetch(f"{self._select} WHERE branch = $1", branch)
        return {r["doc_id"]: _record_from_row(r) for r in rows}

    async def upsert_chapter(self, record: ChapterRecord) -> None:
        await self._pool.execute(
            f"""
            INSERT INTO {self._s}.chapters (doc_id, branch, path, section, title, content,
                content_hash, commit_sha, content_commit_sha, screens, policies, kg_keys)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12::jsonb)
            ON CONFLICT (doc_id) DO UPDATE SET
                branch = EXCLUDED.branch, path = EXCLUDED.path, section = EXCLUDED.section,
                title = EXCLUDED.title, content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash, commit_sha = EXCLUDED.commit_sha,
                content_commit_sha = EXCLUDED.content_commit_sha, screens = EXCLUDED.screens,
                policies = EXCLUDED.policies, kg_keys = EXCLUDED.kg_keys, updated_at = now()
            """,
            record.doc_id,
            record.branch,
            record.path,
            record.section,
            record.title,
            record.content,
            record.content_hash,
            record.commit_sha,
            record.content_commit_sha,
            json.dumps(record.screens, ensure_ascii=False),
            json.dumps(record.policies, ensure_ascii=False),
            record.kg_keys.to_json() if record.kg_keys else None,
        )

    async def touch_chapters(self, doc_ids: list[str], commit_sha: str) -> None:
        if doc_ids:
            await self._pool.execute(
                f"UPDATE {self._s}.chapters SET commit_sha = $2, updated_at = now() "
                "WHERE doc_id = ANY($1::text[])",
                doc_ids,
                commit_sha,
            )

    async def delete_chapter(self, doc_id: str) -> None:
        await self._pool.execute(f"DELETE FROM {self._s}.chapters WHERE doc_id = $1", doc_id)

    async def get_chapter(self, doc_id: str) -> ChapterRecord | None:
        row = await self._pool.fetchrow(f"{self._select} WHERE doc_id = $1", doc_id)
        return _record_from_row(row) if row else None

    async def all_chapters(self) -> list[ChapterRecord]:
        rows = await self._pool.fetch(f"{self._select} ORDER BY doc_id")
        return [_record_from_row(r) for r in rows]

    async def chapters_with_screen(self, screen_id: str) -> list[ChapterRecord]:
        rows = await self._pool.fetch(
            f"{self._select} WHERE screens ? $1 ORDER BY doc_id", screen_id
        )
        return [_record_from_row(r) for r in rows]

    async def chapters_with_policy(self, policy_id: str) -> list[ChapterRecord]:
        rows = await self._pool.fetch(
            f"{self._select} WHERE policies ? $1 ORDER BY doc_id", policy_id
        )
        return [_record_from_row(r) for r in rows]

    async def referenced_names(self, names: list[str]) -> set[str]:
        if not names:
            return set()
        rows = await self._pool.fetch(
            f"""
            SELECT DISTINCT n FROM (
                SELECT jsonb_object_keys(screens) AS n FROM {self._s}.chapters
                UNION ALL SELECT jsonb_object_keys(policies) FROM {self._s}.chapters
                UNION ALL SELECT branch || ':' || path FROM {self._s}.chapters
            ) t WHERE n = ANY($1::text[])
            """,
            names,
        )
        return {r["n"] for r in rows}
