"""인덱서 서비스 — 폴링 1회(``poll_once``)의 전체 흐름 (AC5 – AC9, AC13, AC14).

1. 원격 ``draft/*`` 브랜치 목록 ↔ manifest 의 HEAD 비교(new · changed · deleted · unchanged).
   unchanged 브랜치는 fetch 도 하지 않는다.
2. 삭제된 브랜치 → 그 브랜치의 모든 챕터를 LightRAG · custom KG · manifest 에서 제거.
3. 변경 브랜치 → fetch · 경로 필터 · 챕터 분할 → 챕터 diff.
   해시가 같은 챕터는 LightRAG 를 호출하지 않고 manifest 의 commit_sha 만 갱신한다.
   UTF-8 이 아닌 파일은 WARNING(``event=file_skipped``)을 남기고 건너뛴다.
4. 바뀐 게 있으면 § 참조 · 문서 간 참조를 현재 챕터 집합으로 다시 해석해 custom KG 관계를
   맞춘다(참조하는 챕터가 대상보다 먼저 처리되거나 대상이 나중 주기에 생겨도 REFERS_TO 가
   생긴다. LLM 없음).
   레코드 단위로 실패를 격리하고, 실패가 있으면 변경이 없는 다음 주기에도 다시 시도한다.
5. 바뀐 게 있으면 "0. 용어" 사전으로 별칭 엔티티를 병합한다(실패하면 다음 주기에 다시 시도).

브랜치 단위로 실패를 격리한다: 실패한 브랜치는 HEAD 를 갱신하지 않으므로 다음 주기에 재시도된다.
HEAD 가 없어도 manifest 에 챕터가 남은 브랜치(부분 실패)는 "알려진 브랜치"로 보아, 원격에서
지워지면 삭제하고 남아 있으면 다시 동기화한다.

챕터 삽입은 LightRAG 를 건드리기 **전에** manifest 에 시작 기록(``content_hash`` 가 빈 PENDING
레코드, 이전 · 새 KG 관계를 합친 kg_keys)을 남긴다. 재삽입의 시작 기록은 직전 완료본의 본문 ·
SHA 를 유지하고, 첫 삽입의 시작 기록은 ``content_commit_sha=""`` 로 MCP 조회에서 숨긴다.
그래서 LightRAG 삽입이 FAILED 로 끝나거나 manifest 완료 기록이 실패해도, 다음 주기에 그 챕터를
다시 넣으면서 남은 문서 · 관계를 지우고, 브랜치가 지워지면 함께 정리된다.

LLM 호출은 원인 챕터 doc_id 에 귀속한다(고아 엔티티 삭제는 그 엔티티를 소유했던 챕터). 그래서
``sync_done llm_calls_total`` 은 그 브랜치 ``chapter_sync`` ``llm_calls`` 의 합과 같다.
브랜치 뒷단계가 실패해도 이미 처리한 챕터의 ``chapter_sync`` 는 남긴다(실패한 챕터는
``action=<insert|reinsert>_failed``). 용어 병합의 LLM 호출은 특정 챕터가 아니라
``GLOSSARY_MERGE_OWNER`` 에 귀속한다(``event=glossary_merge llm_calls``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace

from specgraph.blocks import split_blocks
from specgraph.docid import build_doc_id
from specgraph.errors import NonUtf8FileError, SpecGraphError
from specgraph.indexer.chapters import Chapter, split_chapters
from specgraph.indexer.extract import DocumentRef, extract_chapter
from specgraph.indexer.git_source import GitSourcePort
from specgraph.indexer.glossary import Glossary, parse_glossary
from specgraph.indexer.kg import build_chapter_kg
from specgraph.indexer.path_filter import filter_paths
from specgraph.indexer.sync_plan import diff_branches, diff_chapters
from specgraph.kg_model import KgKeys
from specgraph.lightrag_store import LightRagPort, RelationPair, undirected
from specgraph.llm import LlmCallCounter
from specgraph.log import log_event
from specgraph.manifest import PENDING_HASH, ChapterRecord, ManifestPort
from specgraph.settings import DEFAULT_INCLUDE_DIRS

logger = logging.getLogger(__name__)

# HEAD 기록 없이 manifest 에 챕터만 남은 브랜치(부분 실패)의 known HEAD — 어떤 SHA 와도 다르다.
PARTIAL_HEAD = ""
# 용어 병합(amerge_entities)의 LLM 호출 귀속 키 — 챕터 doc_id 가 아니다.
GLOSSARY_MERGE_OWNER = "glossary_merge"

RelationOwners = dict[RelationPair, set[str]]


@dataclass
class PollResult:
    indexed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.indexed or self.removed)


@dataclass(frozen=True)
class _Current:
    path: str
    chapter: Chapter


@dataclass
class _Context:
    """한 브랜치 동기화 동안 쓰는 참조 해석 대상과 관계 소유 현황."""

    documents: list[DocumentRef]
    chapters: set[str]
    owners: RelationOwners


class IndexerService:
    def __init__(
        self,
        git: GitSourcePort,
        rag: LightRagPort,
        manifest: ManifestPort,
        counter: LlmCallCounter,
        include_dirs: Sequence[str] = DEFAULT_INCLUDE_DIRS,
        max_block_chars: int = 1000,
    ) -> None:
        self.git = git
        self.rag = rag
        self.manifest = manifest
        self.counter = counter
        self.include_dirs = tuple(include_dirs)
        self.max_block_chars = max_block_chars
        self._reconcile_pending = False
        self._glossary_pending = False

    # --- 폴링 -----------------------------------------------------------------

    async def poll_once(self) -> PollResult:
        result = PollResult()
        try:
            remote = await self.git.list_branches()
            known = await self._known_branches()
        except SpecGraphError as exc:
            result.error = str(exc)
            log_event(logger, "poll_failed", logging.ERROR, error=str(exc))
            return result

        diff = diff_branches(remote, known)
        result.unchanged = list(diff.unchanged)
        log_event(
            logger,
            "poll",
            branches=len(remote),
            new=len(diff.new),
            changed=len(diff.changed),
            deleted=len(diff.deleted),
            unchanged=len(diff.unchanged),
        )

        for branch in diff.deleted:
            try:
                await self._remove_branch(branch)
                result.removed.append(branch)
            except Exception as exc:  # noqa: BLE001 — 브랜치 단위 격리
                self._branch_failed(result, branch, exc)

        for branch, sha in diff.to_index.items():
            try:
                await self._sync_branch(branch, sha)
                result.indexed.append(branch)
            except Exception as exc:  # noqa: BLE001 — 브랜치 단위 격리
                self._branch_failed(result, branch, exc)

        touched = result.changed or bool(result.failed)
        if touched or self._reconcile_pending:
            self._reconcile_pending = not await self._reconcile_refs()
        if touched or self._glossary_pending:
            try:
                await self._apply_glossary()
                self._glossary_pending = False
            except Exception as exc:  # noqa: BLE001 — 병합 실패가 인덱싱 결과를 무르지 않는다
                self._glossary_pending = True
                log_event(
                    logger,
                    "glossary_failed",
                    logging.ERROR,
                    exc_info=_traceback_for(exc),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        return result

    async def _known_branches(self) -> dict[str, str]:
        known = await self.manifest.branch_heads()
        for branch in await self.manifest.chapter_branches():
            known.setdefault(branch, PARTIAL_HEAD)
        return known

    def _branch_failed(self, result: PollResult, branch: str, exc: Exception) -> None:
        result.failed[branch] = str(exc)
        log_event(
            logger,
            "branch_failed",
            logging.ERROR,
            exc_info=_traceback_for(exc),
            branch=branch,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    # --- 브랜치 동기화 -----------------------------------------------------------

    async def _read_branch(self, branch: str, sha: str) -> dict[str, _Current]:
        await self.git.fetch(branch, sha)
        paths = filter_paths(await self.git.list_files(sha), self.include_dirs)
        current: dict[str, _Current] = {}
        for path in sorted(paths):
            try:
                text = await self.git.read_file(sha, path)
            except NonUtf8FileError as exc:
                log_event(
                    logger,
                    "file_skipped",
                    logging.WARNING,
                    branch=branch,
                    path=path,
                    error=str(exc),
                )
                continue
            for chapter in split_chapters(text):
                current[build_doc_id(branch, path, chapter.section)] = _Current(path, chapter)
        return current

    async def _context(self, branch: str, current: dict[str, _Current]) -> _Context:
        """참조 해석 대상 — 다른 브랜치는 manifest 기준, 이 브랜치는 현재 파일 기준."""
        records = await self.manifest.all_chapters()
        others = [r for r in records if r.branch != branch]
        documents = {DocumentRef(r.branch, r.path) for r in others}
        documents.update(DocumentRef(branch, c.path) for c in current.values())
        chapters = {r.doc_id for r in others if r.content_hash != PENDING_HASH} | set(current)
        return _Context(
            documents=sorted(documents, key=lambda d: (d.branch, d.path)),
            chapters=chapters,
            owners=_relation_owners(records),
        )

    async def _sync_branch(self, branch: str, sha: str) -> None:
        started = time.monotonic()
        llm_before = self.counter.total
        current = await self._read_branch(branch, sha)
        indexed = await self.manifest.chapters_for_branch(branch)
        plan = diff_chapters(
            {d: c.chapter.content_hash for d, c in current.items()},
            {d: r.content_hash for d, r in indexed.items()},
        )
        context = await self._context(branch, current)
        before = {d: self.counter.count_for(d) for d in (*current, *indexed)}
        orphan_candidates: dict[str, str] = {}
        actions: list[tuple[str, str, str]] = []
        reinsert = set(plan.reinsert)

        try:
            for doc_id in plan.delete:
                record = indexed[doc_id]
                # 취소 · 실패로 끝나면 로그에 delete_failed 로 남도록 먼저 그렇게 적는다.
                actions.append((doc_id, "delete_failed", record.content_hash))
                with self.counter.attribute_to(doc_id):
                    await self._delete_chapter(record, context.owners)
                actions[-1] = (doc_id, "delete", record.content_hash)
                _add_owned(orphan_candidates, record)

            for doc_id in sorted([*plan.insert, *plan.reinsert]):
                item = current[doc_id]
                previous = indexed.get(doc_id)
                action = "reinsert" if doc_id in reinsert else "insert"
                actions.append((doc_id, f"{action}_failed", item.chapter.content_hash))
                with self.counter.attribute_to(doc_id):
                    record = await self._insert_chapter(
                        doc_id, branch, sha, item, previous, context
                    )
                actions[-1] = (doc_id, action, record.content_hash)
                if previous is not None:
                    _add_owned(orphan_candidates, previous)

            for doc_id in plan.skip:
                actions.append((doc_id, "skip", current[doc_id].chapter.content_hash))
            await self.manifest.touch_chapters(plan.skip, sha)

            await self._delete_orphans(orphan_candidates)
        finally:
            # 뒷단계가 실패해도 이미 처리(또는 시도)한 챕터의 LLM 호출 수는 남긴다(AC9).
            for doc_id, action, content_hash in actions:
                self._log_chapter(doc_id, action, content_hash, before)

        await self.manifest.set_branch_head(branch, sha)
        log_event(
            logger,
            "sync_done",
            branch=branch,
            head=sha,
            elapsed_s=round(time.monotonic() - started, 3),
            llm_calls_total=self.counter.total - llm_before,
            inserted=len(plan.insert),
            reinserted=len(plan.reinsert),
            deleted=len(plan.delete),
            skipped=len(plan.skip),
        )

    async def _insert_chapter(
        self,
        doc_id: str,
        branch: str,
        sha: str,
        item: _Current,
        previous: ChapterRecord | None,
        context: _Context,
    ) -> ChapterRecord:
        chapter = item.chapter
        refs = extract_chapter(chapter.text)
        payload = build_chapter_kg(doc_id, chapter.title, refs, context.documents, context.chapters)
        record = ChapterRecord(
            doc_id=doc_id,
            branch=branch,
            path=item.path,
            section=chapter.section,
            title=chapter.title,
            content=chapter.text,
            content_hash=chapter.content_hash,
            commit_sha=sha,
            content_commit_sha=sha,
            screens=dict(refs.screen_ranks),
            policies=dict(refs.policy_ranks),
            kg_keys=payload.keys,
        )
        previous_keys = previous.kg_keys if previous else None
        # 시작 기록: 여기서부터 실패하면 다음 주기에 이 챕터를 다시 넣고(이전 · 새 관계를 지운 뒤),
        # 브랜치가 지워지면 LightRAG 문서와 함께 정리된다.
        await self.manifest.upsert_chapter(_start_record(record, previous, payload.keys))
        # LightRAG 는 같은 id 재삽입을 무시하므로 먼저 지운다(없으면 not_found).
        await self.rag.delete_chapter(doc_id)
        await self.rag.insert_chapter(doc_id, split_blocks(chapter.text, self.max_block_chars))
        for ref in payload.unresolved:
            log_event(
                logger,
                "cross_ref_unresolved",
                logging.WARNING,
                doc_id=doc_id,
                name=ref.name,
                kind=ref.kind,
                section=ref.section,
            )
        await self.rag.upsert_chapter_kg(
            payload, previous_keys, keep=_keep_for(doc_id, context.owners)
        )
        _set_owner(context.owners, doc_id, payload.keys)
        await self.manifest.upsert_chapter(record)
        log_event(
            logger,
            "extract",
            doc_id=doc_id,
            screens=len(refs.screens),
            policies=len(refs.policies),
            section_refs=len(refs.section_refs),
            cross_doc_refs=len(refs.cross_doc_refs),
            dangling_refs=len(payload.dangling),
        )
        return record

    async def _delete_chapter(self, record: ChapterRecord, owners: RelationOwners) -> None:
        await self.rag.delete_chapter(record.doc_id)
        if record.kg_keys is not None:
            await self.rag.delete_chapter_kg(record.kg_keys)
        _set_owner(owners, record.doc_id, None)
        await self.manifest.delete_chapter(record.doc_id)

    async def _remove_branch(self, branch: str) -> None:
        started = time.monotonic()
        records = await self.manifest.chapters_for_branch(branch)
        owners = _relation_owners(await self.manifest.all_chapters())
        before = {d: self.counter.count_for(d) for d in records}
        orphan_candidates: dict[str, str] = {}
        done: list[tuple[str, str]] = []
        try:
            for doc_id in sorted(records):
                # 취소 · 실패로 끝나면 delete_failed 로 남도록 먼저 그렇게 적는다.
                done.append((doc_id, "delete_failed"))
                with self.counter.attribute_to(doc_id):
                    await self._delete_chapter(records[doc_id], owners)
                done[-1] = (doc_id, "delete")
                _add_owned(orphan_candidates, records[doc_id])
            await self._delete_orphans(orphan_candidates)
        finally:
            for doc_id, action in done:
                self._log_chapter(doc_id, action, records[doc_id].content_hash, before)
        await self.manifest.delete_branch_head(branch)
        log_event(
            logger,
            "branch_removed",
            branch=branch,
            chapters=len(records),
            elapsed_s=round(time.monotonic() - started, 3),
        )

    async def _delete_orphans(self, candidates: dict[str, str]) -> None:
        """candidates: 엔티티 이름 → 그것을 소유했던 챕터 doc_id(LLM 호출 귀속 대상)."""
        if not candidates:
            return
        names = sorted(candidates)
        still_used = await self.manifest.referenced_names(names)
        by_owner: dict[str, list[str]] = {}
        for name in names:
            if name not in still_used:
                by_owner.setdefault(candidates[name], []).append(name)
        for owner in sorted(by_owner):
            with self.counter.attribute_to(owner):
                await self.rag.delete_entities(by_owner[owner])

    def _log_chapter(
        self, doc_id: str, action: str, content_hash: str, before: dict[str, int]
    ) -> None:
        log_event(
            logger,
            "chapter_sync",
            doc_id=doc_id,
            action=action,
            content_hash=content_hash[:12] or "-",
            llm_calls=self.counter.count_for(doc_id) - before.get(doc_id, 0),
        )

    # --- 참조 재해석 (AC15 · C14) --------------------------------------------------

    async def _reconcile_refs(self) -> bool:
        """§ 참조 · 문서 간 참조가 있는 챕터를 현재 manifest 챕터 집합으로 다시 해석한다(LLM 없음).

        해석 결과(소유 관계)가 manifest 의 kg_keys 와 다를 때만 custom KG 와 kg_keys 를 갱신한다.
        레코드 하나가 실패해도 나머지는 계속한다. 모두 성공하면 True.
        """
        try:
            records = await self.manifest.all_chapters()
        except Exception as exc:  # noqa: BLE001 — 다음 주기에 다시 시도한다
            self._reconcile_failed(None, exc)
            return False
        live = [r for r in records if r.content_hash != PENDING_HASH]
        documents = sorted(
            {DocumentRef(r.branch, r.path) for r in live}, key=lambda d: (d.branch, d.path)
        )
        chapters = {r.doc_id for r in live}
        owners = _relation_owners(records)
        ok = True
        for record in live:
            refs = extract_chapter(record.content)
            if not (refs.cross_doc_refs or refs.section_refs):
                continue
            payload = build_chapter_kg(record.doc_id, record.title, refs, documents, chapters)
            if payload.keys == record.kg_keys:
                continue
            try:
                with self.counter.attribute_to(record.doc_id):
                    await self.rag.upsert_chapter_kg(
                        payload, record.kg_keys, keep=_keep_for(record.doc_id, owners)
                    )
                await self.manifest.upsert_chapter(replace(record, kg_keys=payload.keys))
            except Exception as exc:  # noqa: BLE001 — 레코드 단위 격리
                ok = False
                self._reconcile_failed(record.doc_id, exc)
                continue
            _set_owner(owners, record.doc_id, payload.keys)
            log_event(
                logger,
                "cross_ref_reconciled",
                doc_id=record.doc_id,
                relations=len(payload.keys.relations),
                unresolved=len(payload.unresolved),
                dangling=len(payload.dangling),
            )
        return ok

    @staticmethod
    def _reconcile_failed(doc_id: str | None, exc: BaseException) -> None:
        log_event(
            logger,
            "cross_ref_reconcile_failed",
            logging.ERROR,
            exc_info=exc,
            doc_id=doc_id or "-",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    # --- 용어 사전 -------------------------------------------------------------

    async def _glossary(self) -> Glossary:
        glossary = Glossary()
        for record in await self.manifest.all_chapters():
            if record.section == "0" and record.content_hash != PENDING_HASH:
                glossary = glossary.merge(parse_glossary(split_chapters(record.content)))
        return glossary

    async def _apply_glossary(self) -> None:
        glossary = await self._glossary()
        if not glossary:
            return
        plan = glossary.merge_plan(await self.rag.entity_names())
        for target, sources in plan:
            calls_before = self.counter.count_for(GLOSSARY_MERGE_OWNER)
            with self.counter.attribute_to(GLOSSARY_MERGE_OWNER):
                await self.rag.merge_entities(sources, target)
            log_event(
                logger,
                "glossary_merge",
                target=target,
                sources=",".join(sources),
                llm_calls=self.counter.count_for(GLOSSARY_MERGE_OWNER) - calls_before,
            )


def _traceback_for(exc: BaseException) -> BaseException | None:
    """도메인 예외(SpecGraphError)는 메시지로 충분하다. 예상 못 한 예외만 traceback 을 남긴다."""
    return None if isinstance(exc, SpecGraphError) else exc


def _start_record(
    record: ChapterRecord, previous: ChapterRecord | None, new_keys: KgKeys
) -> ChapterRecord:
    """삽입 시작 기록(PENDING).

    직전에 완료된 레코드가 있으면(재삽입) 그 본문 · SHA · 화면/정책을 유지해 MCP 조회가 직전
    성공 상태를 계속 보이게 한다. 완료된 적이 없으면(첫 삽입, 또는 첫 삽입 재시도) 새 값을
    담되 ``content_commit_sha`` 를 비워 숨김 대상으로 만든다.
    """
    keys = _pending_keys(previous.kg_keys if previous else None, new_keys)
    if previous is not None and not previous.is_hidden:
        return replace(previous, content_hash=PENDING_HASH, kg_keys=keys)
    return replace(record, content_hash=PENDING_HASH, content_commit_sha="", kg_keys=keys)


def _pending_keys(previous: KgKeys | None, new: KgKeys) -> KgKeys:
    """시작 기록의 kg_keys — 다음 시도가 이전 · 이번 시도의 관계와 앵커 청크를 지울 수 있게 합친다.

    ``anchor_content`` 는 직전 완료본의 앵커로 두고(아직 LightRAG 에 있다), 이전 PENDING 이
    기억하던 중간 앵커와 이번 시도의 새 앵커는 ``stale_anchors`` 에 쌓는다.
    """
    if previous is None:
        return new
    relations = tuple(dict.fromkeys((*previous.relations, *new.relations)))
    stale = tuple(
        a
        for a in dict.fromkeys((*previous.stale_anchors, new.anchor_content))
        if a != previous.anchor_content
    )
    return KgKeys(new.chapter_entity, relations, previous.anchor_content, stale)


def _relation_owners(records: Collection[ChapterRecord]) -> RelationOwners:
    owners: RelationOwners = {}
    for record in records:
        if record.kg_keys is not None:
            for pair in record.kg_keys.relations:
                owners.setdefault(undirected(pair), set()).add(record.doc_id)
    return owners


def _set_owner(owners: RelationOwners, doc_id: str, keys: KgKeys | None) -> None:
    for who in owners.values():
        who.discard(doc_id)
    if keys is not None:
        for pair in keys.relations:
            owners.setdefault(undirected(pair), set()).add(doc_id)


def _keep_for(doc_id: str, owners: RelationOwners) -> frozenset[RelationPair]:
    """이 챕터가 닿아 있으면서 다른 챕터도 소유한 무방향 관계 — 지우지 않는다(C9)."""
    return frozenset(pair for pair, who in owners.items() if doc_id in pair and who - {doc_id})


def _owned_names(record: ChapterRecord) -> set[str]:
    return {*record.screens, *record.policies, record.document}


def _add_owned(candidates: dict[str, str], record: ChapterRecord) -> None:
    for name in sorted(_owned_names(record)):
        candidates.setdefault(name, record.doc_id)
