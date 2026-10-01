"""doc_id 규칙 — ``<branch>:<path>#<§번호>`` (티켓 고정, AC8).

git refname 에는 ``:`` 이 올 수 없으므로 첫 ``:`` 이 브랜치와 경로의 경계이고,
마지막 ``#`` 이 경로와 섹션의 경계다.
"""

from __future__ import annotations

from dataclasses import dataclass

# 첫 번호 헤딩 앞 머리말의 섹션 값(결정 D5) — ``<branch>:<path>#preamble``.
PREAMBLE = "preamble"


@dataclass(frozen=True)
class DocId:
    branch: str
    path: str
    section: str

    @property
    def value(self) -> str:
        return build_doc_id(self.branch, self.path, self.section)

    @property
    def document(self) -> str:
        return f"{self.branch}:{self.path}"


def build_doc_id(branch: str, path: str, section: str) -> str:
    return f"{branch}:{path}#{section}"


def parse_doc_id(doc_id: str) -> DocId:
    branch, sep, rest = doc_id.partition(":")
    path, hash_sep, section = rest.rpartition("#")
    if not (sep and hash_sep and branch and path and section):
        raise ValueError(f"doc_id 형식이 아니다: {doc_id!r}")
    return DocId(branch=branch, path=path, section=section)


def branch_prefix(branch: str) -> str:
    return f"{branch}:"


def belongs_to_branch(doc_id: str, branch: str) -> bool:
    return doc_id.startswith(branch_prefix(branch))


def document_key(doc_id: str) -> str:
    return parse_doc_id(doc_id).document
