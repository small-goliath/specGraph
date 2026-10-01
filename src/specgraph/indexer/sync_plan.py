"""동기화 계획 — 브랜치 diff 와 챕터 diff (AC5 · AC6 · AC7 · AC9). 순수 함수."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BranchDiff:
    new: dict[str, str] = field(default_factory=dict)
    changed: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def to_index(self) -> dict[str, str]:
        return {**self.new, **self.changed}


def diff_branches(remote: Mapping[str, str], known: Mapping[str, str]) -> BranchDiff:
    """remote: 원격 브랜치 → HEAD, known: 마지막으로 인덱싱을 마친 브랜치 → HEAD."""
    new = {b: sha for b, sha in sorted(remote.items()) if b not in known}
    changed = {b: sha for b, sha in sorted(remote.items()) if b in known and known[b] != sha}
    unchanged = sorted(b for b, sha in remote.items() if known.get(b) == sha)
    deleted = sorted(b for b in known if b not in remote)
    return BranchDiff(new=new, changed=changed, deleted=deleted, unchanged=unchanged)


@dataclass(frozen=True)
class ChapterPlan:
    insert: list[str] = field(default_factory=list)
    reinsert: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return len(self.insert) + len(self.reinsert) + len(self.delete)


def diff_chapters(current: Mapping[str, str], indexed: Mapping[str, str]) -> ChapterPlan:
    """current/indexed: doc_id → content_hash. 해시가 같은 챕터는 skip(LightRAG 무접촉)."""
    return ChapterPlan(
        insert=sorted(d for d in current if d not in indexed),
        reinsert=sorted(d for d, h in current.items() if d in indexed and indexed[d] != h),
        delete=sorted(d for d in indexed if d not in current),
        skip=sorted(d for d, h in current.items() if indexed.get(d) == h),
    )
