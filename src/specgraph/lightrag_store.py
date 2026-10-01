"""LightRAG 포트와 어댑터 — LightRAG(lightrag-hku) API 차이는 이 파일에서만 흡수한다.

doc_id ↔ LightRAG ``file_path``
    LightRAG 1.5 는 ``file_path`` 를 basename 으로 정규화한다(``Path(p).name``). doc_id 에는
    ``/`` 가 있으므로 그대로 넣으면 브랜치 정보가 사라진다. 그래서 doc_id 를 URL 인코딩(``/`` →
    ``%2F``)해 넣고, 검색 결과에서 다시 디코딩해 출처 doc_id 를 복원한다.

챕터 블록 (결정 D7, AC10)
    ``blocks.join_blocks`` 결과를 ``split_by_character=BLOCK_SEPARATOR`` +
    ``split_by_character_only=True`` 로 넘겨, LightRAG 가 블록 안(표 안)을 토큰 기준으로 다시
    자르지 않게 한다. 넘기기 전에 LightRAG 토크나이저로 블록을 재고, ``chunk_token_size``
    (``CHUNK_SIZE``, Settings) 를 넘는 블록은 ``fit_blocks`` 로 다시 나눈다(표는 행 경계 · 헤더
    반복). 그래도 한도를 넘는 블록(단일 행 · 긴 문단)이 남으면 WARNING
    (``event=block_over_token_limit``)을 남기고 그 챕터만 ``split_by_character_only=False`` 로
    넣는다(LightRAG 는 한도 초과 블록에서 예외를 낸다). 본문 조립은 ``build_insert_body`` 한 곳이다.

본문 해시 dedup 회피
    LightRAG 는 doc_id 가 달라도 본문 해시가 같으면 그 문서의 상태 레코드를 만들지 않는다.
    템플릿 챕터처럼 본문이 같은 챕터가 서로 다른 문서에 있을 수 있으므로, 첫 줄에 doc_id 해시로
    만든 머리 줄(``<!-- specgraph:<16hex> -->``)을 붙여 본문을 doc_id 마다 유일하게 만든다.
    doc_id 자체를 쓰지 않는 이유: LLM 이 그것을 엔티티로 추출하면 custom KG 의 CHAPTER 엔티티
    (이름 = doc_id)와 충돌한다.

custom KG 의 공유 노드 (SCREEN · POLICY · DOCUMENT)
    LightRAG ``ainsert_custom_kg`` 는 노드를 병합 없이 덮어쓴다. 그래서 이미 있는 공유 노드는 삽입
    뒤에 기존 설명 · source_id · file_path 와 합쳐 되돌린다(``aedit_entity``, LLM 없음). 챕터 KG 를
    지우거나 바꿀 때는 그 챕터의 앵커 청크 · file_path 만 공유 노드에서 뺀다. LightRAG 엣지는
    무방향이므로, 다른 챕터가 소유한 같은 엣지(``keep``)는 지우지 않는다.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol
from urllib.parse import quote, unquote

from specgraph.blocks import BLOCK_SEPARATOR, Measure, fit_blocks, join_blocks
from specgraph.docid import parse_doc_id
from specgraph.errors import ConfigError, IndexingError
from specgraph.kg_model import KgKeys, KgPayload
from specgraph.llm import LlmCallCounter, make_llm_func, openai_compatible_embed
from specgraph.log import log_event
from specgraph.settings import Settings

logger = logging.getLogger(__name__)

GRAPH_FIELD_SEP = "<SEP>"
QUERY_MODES = ("local", "global", "hybrid", "naive", "mix")
_PLACEHOLDER = "UNKNOWN"  # LightRAG 가 없는 노드를 자동 생성할 때 쓰는 값
_STORAGE_ATTRS = (
    "full_docs",
    "text_chunks",
    "full_entities",
    "full_relations",
    "entity_chunks",
    "relation_chunks",
    "entities_vdb",
    "relationships_vdb",
    "chunks_vdb",
    "chunk_entity_relation_graph",
    "llm_response_cache",
    "doc_status",
)


def encode_source(doc_id: str) -> str:
    return quote(doc_id, safe="")


def doc_header(doc_id: str) -> str:
    """doc_id 마다 다른 머리 줄. doc_id 문자열 자체는 담지 않는다(엔티티 이름 충돌 방지)."""
    digest = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:16]
    return f"<!-- specgraph:{digest} -->"


@dataclass(frozen=True)
class InsertBody:
    text: str
    oversized: list[int]  # 재분할 뒤에도 한도를 넘는 블록들의 크기(measure 단위)


def build_insert_body(doc_id: str, blocks: list[str], limit: int, measure: Measure) -> InsertBody:
    """``ainsert`` 에 넘기는 본문을 만든다(머리 줄 + 한도 재분할 + 블록 구분자 결합)."""
    header = doc_header(doc_id)
    headed = [f"{header}\n{blocks[0]}", *blocks[1:]] if blocks else [header]
    fitted = fit_blocks(headed, limit, measure)
    oversized = [n for n in map(measure, fitted) if n > limit]
    return InsertBody(text=join_blocks(fitted), oversized=oversized)


def decode_sources(file_path: str | None) -> list[str]:
    """LightRAG file_path 필드(``<SEP>`` 로 여러 개일 수 있음) → doc_id 목록."""
    result: list[str] = []
    for part in (file_path or "").split(GRAPH_FIELD_SEP):
        candidate = unquote(part.strip())
        try:
            parse_doc_id(candidate)
        except ValueError:
            continue
        if candidate not in result:
            result.append(candidate)
    return result


@dataclass(frozen=True)
class RetrievedChunk:
    doc_id: str
    content: str


@dataclass(frozen=True)
class RetrievedEntity:
    name: str
    entity_type: str
    description: str
    doc_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrievedRelation:
    src: str
    tgt: str
    keywords: str
    description: str
    doc_ids: tuple[str, ...]


@dataclass
class Retrieval:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    entities: list[RetrievedEntity] = field(default_factory=list)
    relations: list[RetrievedRelation] = field(default_factory=list)


RelationPair = tuple[str, str]


class LightRagPort(Protocol):
    """``keep`` 은 다른 챕터가 소유한 관계(무방향 쌍)다. 이 챕터의 KG 를 바꿀 때 그 엣지는
    지우지 않는다(챕터 KG 를 지울 때는 챕터 엔티티와 함께 모든 엣지가 사라지는 것이 맞다)."""

    async def insert_chapter(self, doc_id: str, blocks: list[str]) -> None: ...

    async def delete_chapter(self, doc_id: str) -> None: ...

    async def upsert_chapter_kg(
        self,
        payload: KgPayload,
        previous: KgKeys | None,
        keep: frozenset[RelationPair] = frozenset(),
    ) -> None: ...

    async def delete_chapter_kg(self, keys: KgKeys) -> None: ...

    async def delete_entities(self, names: list[str]) -> None: ...

    async def entity_names(self) -> list[str]: ...

    async def merge_entities(self, sources: list[str], target: str) -> None: ...

    async def retrieve(self, question: str, mode: str, top_k: int | None = None) -> Retrieval: ...


def to_retrieval(data: dict[str, Any]) -> Retrieval:
    """``aquery_data`` 결과(data 섹션) → Retrieval. 출처를 알 수 없는 항목은 버린다."""
    section = data.get("data") or {}
    chunks = [
        RetrievedChunk(doc_id=ids[0], content=c.get("content", ""))
        for c in section.get("chunks", [])
        if (ids := decode_sources(c.get("file_path")))
    ]
    entities = [
        RetrievedEntity(
            name=e.get("entity_name", ""),
            entity_type=e.get("entity_type", ""),
            description=e.get("description", ""),
            doc_ids=tuple(ids),
        )
        for e in section.get("entities", [])
        if (ids := decode_sources(e.get("file_path")))
    ]
    relations = [
        RetrievedRelation(
            src=r.get("src_id", ""),
            tgt=r.get("tgt_id", ""),
            keywords=r.get("keywords", ""),
            description=r.get("description", ""),
            doc_ids=tuple(ids),
        )
        for r in section.get("relationships", [])
        if (ids := decode_sources(r.get("file_path")))
    ]
    return Retrieval(chunks=chunks, entities=entities, relations=relations)


def encode_payload(payload: KgPayload) -> dict[str, Any]:
    custom = copy.deepcopy(payload.custom_kg)
    for key in ("chunks", "entities", "relationships"):
        for item in custom.get(key, []):
            if "file_path" in item:
                item["file_path"] = encode_source(item["file_path"])
    return custom


def make_embedding_func(settings: Settings) -> Any:
    """임베딩 함수(LightRAG ``EmbeddingFunc``). 모델명 · 엔드포인트는 Settings 에서만 온다."""
    from lightrag.utils import EmbeddingFunc

    if settings.embedding_binding == "openai":
        func = partial(
            openai_compatible_embed,
            model=settings.embedding_model,
            base_url=settings.embedding_binding_host,
        )
    else:
        from lightrag.llm.ollama import ollama_embed

        func = partial(
            ollama_embed.func,
            embed_model=settings.embedding_model,
            host=settings.embedding_binding_host,
        )
    return EmbeddingFunc(
        embedding_dim=settings.embedding_dim,
        func=func,
        max_token_size=8192,
        model_name=settings.embedding_model,
    )


def make_rerank_func(settings: Settings) -> Any | None:
    if not settings.rerank_model:
        return None
    if not settings.rerank_binding_host:
        raise ConfigError("RERANK_MODEL 을 쓰려면 RERANK_BINDING_HOST 가 필요하다")
    from lightrag.rerank import cohere_rerank

    # LightRAG 서버와 같은 규약: RERANK_BINDING_HOST 는 rerank 엔드포인트 전체 URL 이다.
    return partial(
        cohere_rerank, model=settings.rerank_model, base_url=settings.rerank_binding_host
    )


def undirected(pair: Iterable[str]) -> RelationPair:
    src, tgt = sorted(pair)
    return src, tgt


def _split_field(value: Any) -> list[str]:
    return [p for p in str(value or "").split(GRAPH_FIELD_SEP) if p]


def _join_field(parts: Iterable[str]) -> str:
    return GRAPH_FIELD_SEP.join(dict.fromkeys(p for p in parts if p))


def _anchor_chunk_id(anchor_content: str) -> str:
    from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding

    return compute_mdhash_id(sanitize_text_for_encoding(anchor_content), prefix="chunk-")


def _ensure_deleted(result: Any, what: str) -> None:
    """LightRAG 삭제 결과(``DeletionResult``)가 success · not_found 가 아니면 실패로 올린다.

    LightRAG 는 삭제 실패를 예외 대신 ``status="fail"`` 로 돌려준다. 삼키면 재시도 경로가
    실패를 알 수 없어 엣지 · 엔티티가 영구 고아로 남는다. not_found 는 이미 지워진 것이므로
    정상(재시도 멱등)이다.
    """
    status = getattr(result, "status", None)
    if status not in ("success", "not_found"):
        raise IndexingError(f"LightRAG 삭제 실패 {what}: status={status} {result.message}")


def _status_fields(status: Any) -> tuple[str | None, str | None]:
    """doc status 는 스토리지에 따라 dict 또는 DocProcessingStatus 로 온다."""
    if status is None:
        return None, None
    if isinstance(status, dict):
        raw, error = status.get("status"), status.get("error_msg")
    else:
        raw, error = getattr(status, "status", None), getattr(status, "error_msg", None)
    return str(getattr(raw, "value", raw)) if raw is not None else None, error


class LightRagStore:
    """LightRagPort 의 실제 구현. ``create()`` 로 만들고 ``close()`` 로 닫는다."""

    def __init__(self, rag: Any) -> None:
        self.rag = rag

    @classmethod
    async def create(
        cls,
        settings: Settings,
        counter: LlmCallCounter,
        **overrides: Any,
    ) -> LightRagStore:
        from lightrag import LightRAG

        settings.working_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "working_dir": str(settings.working_dir),
            "kv_storage": settings.kv_storage,
            "vector_storage": settings.vector_storage,
            "graph_storage": settings.graph_storage,
            "doc_status_storage": settings.doc_status_storage,
            "llm_model_func": make_llm_func(settings, counter),
            "llm_model_name": settings.llm_model,
            "embedding_func": make_embedding_func(settings),
            "rerank_model_func": make_rerank_func(settings),
            "addon_params": settings.lightrag_addon_params(),
            "chunk_token_size": settings.chunk_token_size,
            "enable_llm_cache_for_entity_extract": True,
        }
        kwargs.update(overrides)
        rag = LightRAG(**kwargs)
        await rag.initialize_storages()
        return cls(rag)

    async def close(self) -> None:
        await self.rag.finalize_storages()

    async def drop_workspace(self) -> None:
        """이 workspace 의 모든 LightRAG 스토리지를 비운다(통합 테스트 정리용, 운영에서 안 씀).

        하나라도 실패하면 나머지를 마저 비운 뒤 ``IndexingError`` 로 알린다(실패를 삼키지 않는다).
        """
        failures: list[str] = []
        for attr in _STORAGE_ATTRS:
            storage = getattr(self.rag, attr, None)
            if storage is None or not hasattr(storage, "drop"):
                continue
            try:
                result = await storage.drop()
            except Exception as exc:  # noqa: BLE001 — 모아서 한 번에 알린다
                failures.append(f"{attr}: {exc}")
                continue
            if isinstance(result, dict) and result.get("status") == "error":
                failures.append(f"{attr}: {result.get('message')}")
        if failures:
            raise IndexingError("LightRAG workspace 정리 실패 — " + "; ".join(failures))

    def _token_length(self, text: str) -> int:
        return len(self.rag.tokenizer.encode(text))

    async def insert_chapter(self, doc_id: str, blocks: list[str]) -> None:
        limit = int(self.rag.chunk_token_size)
        body = build_insert_body(doc_id, blocks, limit, self._token_length)
        if body.oversized:
            log_event(
                logger,
                "block_over_token_limit",
                logging.WARNING,
                doc_id=doc_id,
                blocks=len(body.oversized),
                max_tokens=max(body.oversized),
                limit=limit,
            )
        await self.rag.ainsert(
            body.text,
            split_by_character=BLOCK_SEPARATOR,
            split_by_character_only=not body.oversized,
            ids=[doc_id],
            file_paths=[encode_source(doc_id)],
        )
        statuses = await self.rag.aget_docs_by_ids([doc_id])
        state, error = _status_fields(statuses.get(doc_id))
        if state != "processed":
            raise IndexingError(f"LightRAG 삽입 실패 doc_id={doc_id} status={state} error={error}")

    async def delete_chapter(self, doc_id: str) -> None:
        _ensure_deleted(await self.rag.adelete_by_doc_id(doc_id), f"doc_id={doc_id}")

    async def _remove_relations(
        self, relations: Iterable[RelationPair], keep: frozenset[RelationPair] = frozenset()
    ) -> None:
        kept = {undirected(pair) for pair in keep}
        for src, tgt in relations:
            if undirected((src, tgt)) not in kept:
                _ensure_deleted(
                    await self.rag.adelete_by_relation(src, tgt), f"relation={src}~{tgt}"
                )

    async def _delete_anchor_chunks(self, anchors: Iterable[str]) -> None:
        chunk_ids = [_anchor_chunk_id(a) for a in dict.fromkeys(anchors)]
        if chunk_ids:
            await self.rag.chunks_vdb.delete(chunk_ids)
            await self.rag.text_chunks.delete(chunk_ids)

    async def _detach(self, keys: KgKeys, names: Iterable[str]) -> None:
        """공유 노드에서 이 챕터의 앵커 청크 · file_path 만 뺀다(다른 출처가 남은 노드만).

        이 챕터만 출처인 노드는 건드리지 않는다 — 고아 엔티티 정리(서비스)가 지운다.
        """
        chunk_ids = {_anchor_chunk_id(a) for a in keys.all_anchors}
        own_path = encode_source(keys.chapter_entity)
        graph = self.rag.chunk_entity_relation_graph
        for name in sorted(set(names)):
            node = await graph.get_node(name)
            if not node:
                continue
            sources = _split_field(node.get("source_id"))
            paths = _split_field(node.get("file_path"))
            rest_sources = [s for s in sources if s not in chunk_ids]
            rest_paths = [p for p in paths if p != own_path]
            if (rest_sources == sources and rest_paths == paths) or not rest_sources:
                continue
            update = {"source_id": _join_field(rest_sources)}
            if rest_paths:
                update["file_path"] = _join_field(rest_paths)
            await self.rag.aedit_entity(name, update, allow_rename=False)

    async def _merge_with_existing(
        self, existing: dict[str, dict[str, Any]], stale_chunks: set[str]
    ) -> None:
        """``ainsert_custom_kg`` 가 덮어쓴 공유 노드에 삽입 전 내용을 합쳐 되돌린다(LLM 없음)."""
        graph = self.rag.chunk_entity_relation_graph
        for name, before in existing.items():
            after = await graph.get_node(name)
            if not after:
                continue
            descriptions = [d for d in _split_field(before.get("description")) if d != _PLACEHOLDER]
            ours = str(after.get("description") or "")
            if ours and ours not in descriptions:
                descriptions.append(ours)
            sources = [
                s
                for s in (
                    *_split_field(before.get("source_id")),
                    *_split_field(after.get("source_id")),
                )
                if s != _PLACEHOLDER and s not in stale_chunks
            ]
            paths = [*_split_field(before.get("file_path")), *_split_field(after.get("file_path"))]
            await self.rag.aedit_entity(
                name,
                {
                    "description": _join_field(descriptions) or ours,
                    "source_id": _join_field(sources),
                    "file_path": _join_field(paths),
                },
                allow_rename=False,
            )

    async def upsert_chapter_kg(
        self,
        payload: KgPayload,
        previous: KgKeys | None,
        keep: frozenset[RelationPair] = frozenset(),
    ) -> None:
        keys = payload.keys
        stale_chunks: set[str] = set()
        if previous is not None:
            await self._remove_relations(previous.relations, keep)
            dropped = {t for _, t in previous.relations} - {t for _, t in keys.relations}
            await self._detach(previous, dropped)
            # 완료 앵커를 뺀 모든 이전 앵커(직전 완료본 + 중간 시도들)의 청크를 지운다.
            old_anchors = [a for a in previous.all_anchors if a != keys.anchor_content]
            await self._delete_anchor_chunks(old_anchors)
            stale_chunks.update(_anchor_chunk_id(a) for a in old_anchors)
        graph = self.rag.chunk_entity_relation_graph
        existing: dict[str, dict[str, Any]] = {}
        for entity in payload.custom_kg["entities"]:
            name = entity["entity_name"]
            if name != keys.chapter_entity and (node := await graph.get_node(name)):
                existing[name] = dict(node)
        await self.rag.ainsert_custom_kg(
            encode_payload(payload), full_doc_id=f"kg:{keys.chapter_entity}"
        )
        await self._merge_with_existing(existing, stale_chunks)

    async def delete_chapter_kg(self, keys: KgKeys) -> None:
        await self._remove_relations(keys.relations)
        await self._detach(keys, (t for _, t in keys.relations))
        _ensure_deleted(
            await self.rag.adelete_by_entity(keys.chapter_entity), f"entity={keys.chapter_entity}"
        )
        await self._delete_anchor_chunks(keys.all_anchors)

    async def delete_entities(self, names: list[str]) -> None:
        for name in names:
            _ensure_deleted(await self.rag.adelete_by_entity(name), f"entity={name}")

    async def entity_names(self) -> list[str]:
        return list(await self.rag.chunk_entity_relation_graph.get_all_labels())

    async def merge_entities(self, sources: list[str], target: str) -> None:
        await self.rag.amerge_entities(sources, target)

    async def retrieve(self, question: str, mode: str, top_k: int | None = None) -> Retrieval:
        from lightrag import QueryParam

        param = QueryParam(mode=mode)  # type: ignore[arg-type]
        if top_k is not None:
            param.top_k = top_k
        return to_retrieval(await self.rag.aquery_data(question, param))
