"""manifest 직접 조회 도구 — LLM 없음 (AC17 · AC19).

모든 응답에 doc_id 와 commit_sha 를 담는다. 화면 · 정책의 "정의" 챕터는
헤딩 > 표 첫 열 > 최초 등장 순으로 고른다(동률이면 문서 · 챕터 순서상 먼저).
"""

from __future__ import annotations

import re
from typing import Any

from specgraph.docid import PREAMBLE
from specgraph.errors import NotFoundError
from specgraph.kg_model import choose_definition
from specgraph.manifest import ChapterRecord, ManifestPort

_SECTION = re.compile(r"^§?\s*(\d+)")


def _chapter_order(record: ChapterRecord) -> tuple[str, str, int]:
    section = -1 if record.section == PREAMBLE else int(record.section)
    return record.branch, record.path, section


def _tokens(value: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", value.lower()) if t]


def _contains(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i : i + n] == needle for i in range(len(haystack)))


def _normalize_section(section: str) -> str | None:
    text = (section or "").strip()
    if not text:
        return None
    if text.lower() == PREAMBLE:
        return PREAMBLE
    m = _SECTION.match(text)
    return m.group(1) if m else text


def _chapter_dict(record: ChapterRecord) -> dict[str, Any]:
    return {
        "doc_id": record.doc_id,
        "commit_sha": record.commit_sha,
        "branch": record.branch,
        "path": record.path,
        "section": record.section,
        "title": record.title,
        "content": record.content,
    }


def _visible(records: list[ChapterRecord]) -> list[ChapterRecord]:
    """삽입이 한 번도 완료되지 않은 PENDING 레코드는 LightRAG 에 없으므로 보이지 않는다(D6)."""
    return [r for r in records if not r.is_hidden]


class LookupService:
    def __init__(self, manifest: ManifestPort) -> None:
        self.manifest = manifest

    async def _find_definition(
        self, identifier: str, records: list[ChapterRecord], kind: str
    ) -> dict[str, Any]:
        attr = "screens" if kind == "screen" else "policies"
        ordered = sorted(records, key=_chapter_order)
        candidates = [(r.doc_id, getattr(r, attr)[identifier]) for r in ordered]
        chosen = choose_definition(candidates)
        if chosen is None:
            raise NotFoundError(f"{kind} {identifier} 를 정의하거나 언급한 챕터가 없다")
        record = next(r for r in ordered if r.doc_id == chosen)
        result = {"id": identifier, **_chapter_dict(record)}
        result["mentioned_in"] = [r.doc_id for r in ordered if r.doc_id != chosen]
        return result

    async def find_screen(self, screen_id: str) -> dict[str, Any]:
        identifier = screen_id.strip().upper()
        records = _visible(await self.manifest.chapters_with_screen(identifier))
        return await self._find_definition(identifier, records, "screen")

    async def find_policy(self, policy_id: str) -> dict[str, Any]:
        identifier = policy_id.strip().upper()
        records = _visible(await self.manifest.chapters_with_policy(identifier))
        return await self._find_definition(identifier, records, "policy")

    async def _resolve_document(self, doc: str, records: list[ChapterRecord]) -> list[str]:
        documents = sorted({r.document for r in records})
        needle = doc.strip()
        exact = [d for d in documents if d == needle]
        if exact:
            return exact
        by_branch = [d for d in documents if d.split(":", 1)[0] == needle]
        if by_branch:
            return by_branch
        tokens = _tokens(needle)
        matched = []
        for d in documents:
            branch, path = d.split(":", 1)
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if _contains(_tokens(stem), tokens) or _contains(
                _tokens(branch.rsplit("/", 1)[-1]), tokens
            ):
                matched.append(d)
        return matched

    async def get_chapter(self, doc: str, section: str) -> dict[str, Any]:
        records = _visible(await self.manifest.all_chapters())
        if "#" in doc and ":" in doc:
            doc, _, embedded = doc.rpartition("#")
            section = section or embedded
        wanted = _normalize_section(section)
        if wanted is None:
            raise NotFoundError("section 이 비어 있다")
        documents = await self._resolve_document(doc, records)
        if not documents:
            raise NotFoundError(f"문서 {doc!r} 를 찾지 못했다")
        if len(documents) > 1:
            raise NotFoundError(f"문서 {doc!r} 가 여러 개와 맞는다: {', '.join(documents)}")
        doc_id = f"{documents[0]}#{wanted}"
        record = next((r for r in records if r.doc_id == doc_id), None)
        if record is None:
            raise NotFoundError(f"챕터 {doc_id} 가 없다")
        return _chapter_dict(record)

    async def list_docs(self) -> dict[str, Any]:
        """브랜치 commit_sha 는 manifest 의 브랜치 HEAD(없으면 — 부분 실패 — 첫 챕터의 SHA)."""
        records = sorted(_visible(await self.manifest.all_chapters()), key=_chapter_order)
        heads = await self.manifest.branch_heads()
        branches: dict[str, dict[str, Any]] = {}
        for record in records:
            branch = branches.setdefault(
                record.branch,
                {
                    "branch": record.branch,
                    "commit_sha": heads.get(record.branch, record.commit_sha),
                    "documents": {},
                },
            )
            document = branch["documents"].setdefault(
                record.path, {"path": record.path, "doc": record.document, "chapters": []}
            )
            document["chapters"].append(
                {
                    "section": record.section,
                    "title": record.title,
                    "doc_id": record.doc_id,
                    "commit_sha": record.commit_sha,
                }
            )
        return {
            "branches": [
                {**b, "documents": list(b["documents"].values())} for b in branches.values()
            ]
        }
