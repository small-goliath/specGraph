"""공통 픽스처 — 환경변수 격리, 로컬 bare git 원격, 합성 문서 경로."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

FIXTURE_DOCS = Path(__file__).parent / "fixtures" / "docs"

# Settings 가 읽는 모든 환경변수. 개발자 셸의 값이 테스트로 새지 않도록 매번 지운다.
SETTINGS_ENV_NAMES = (
    "SPECGRAPH_REPO_URL",
    "SPECGRAPH_GIT_TOKEN",
    "SPECGRAPH_BRANCH_GLOB",
    "SPECGRAPH_INCLUDE_DIRS",
    "SPECGRAPH_POLL_INTERVAL_SECONDS",
    "SPECGRAPH_CACHE_DIR",
    "SPECGRAPH_WORKING_DIR",
    "SPECGRAPH_LOG_FORMAT",
    "SPECGRAPH_LOG_LEVEL",
    "SPECGRAPH_MAX_BLOCK_CHARS",
    "SPECGRAPH_GIT_TIMEOUT_SECONDS",
    "LLM_MODEL",
    "LLM_BINDING_HOST",
    "LLM_TIMEOUT",
    "OLLAMA_LLM_NUM_CTX",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_BINDING_HOST",
    "EMBEDDING_BINDING",
    "RERANK_BINDING",
    "RERANK_MODEL",
    "RERANK_BINDING_HOST",
    "SUMMARY_LANGUAGE",
    "CHUNK_SIZE",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "LIGHTRAG_KV_STORAGE",
    "LIGHTRAG_VECTOR_STORAGE",
    "LIGHTRAG_GRAPH_STORAGE",
    "LIGHTRAG_DOC_STATUS_STORAGE",
)

BASE_ENV = {
    "SPECGRAPH_REPO_URL": "file:///tmp/product-docs.git",
    "LLM_MODEL": "test-llm:1b",
    "EMBEDDING_MODEL": "test-embed",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in SETTINGS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def settings_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """필수 값만 채운 환경. 테스트가 추가로 setenv 해서 덮어쓴다."""
    for name, value in BASE_ENV.items():
        clean_env.setenv(name, value)
    return clean_env


@pytest.fixture
def settings(settings_env: pytest.MonkeyPatch, tmp_path: Path):
    from specgraph.settings import load_settings

    settings_env.setenv("SPECGRAPH_CACHE_DIR", str(tmp_path / "cache"))
    settings_env.setenv("SPECGRAPH_WORKING_DIR", str(tmp_path / "rag"))
    return load_settings()


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@dataclass
class GitRemote:
    """로컬 bare 저장소와 그것에 푸시하는 작업 사본."""

    bare: Path
    work: Path
    shas: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(self, branch: str, files: dict[str, str | bytes | None], message: str = "c") -> str:
        """branch 를 (없으면 고아 브랜치로) 체크아웃해 files 를 쓰고(None 이면 삭제) 푸시한다.

        bytes 값은 그대로 쓴다(UTF-8 이 아닌 파일 재현용)."""
        existing = _git(self.work, "branch", "--list", branch)
        if existing:
            _git(self.work, "checkout", "-q", branch)
        else:
            _git(self.work, "checkout", "-q", "--orphan", branch)
            _git(self.work, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
            for child in self.work.iterdir():
                if child.name != ".git":
                    _remove(child)
        for rel, content in files.items():
            target = self.work / rel
            if content is None:
                if target.exists():
                    _git(self.work, "rm", "-q", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
            _git(self.work, "add", rel)
        _git(self.work, "commit", "-q", "--allow-empty", "-m", message)
        _git(self.work, "push", "-q", "-f", "origin", f"{branch}:refs/heads/{branch}")
        sha = _git(self.work, "rev-parse", "HEAD")
        self.shas[branch] = sha
        return sha

    def delete_branch(self, branch: str) -> None:
        _git(self.work, "push", "-q", "origin", f":refs/heads/{branch}")
        self.shas.pop(branch, None)


def _remove(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    else:
        path.unlink()


@pytest.fixture
def git_remote(tmp_path: Path) -> Iterator[Callable[[], GitRemote]]:
    """호출할 때마다 새 bare 원격을 만든다."""
    created: list[GitRemote] = []

    def factory() -> GitRemote:
        idx = len(created)
        bare = tmp_path / f"remote{idx}.git"
        work = tmp_path / f"work{idx}"
        bare.mkdir()
        work.mkdir()
        _git(bare, "init", "-q", "--bare")
        _git(work, "init", "-q")
        _git(work, "remote", "add", "origin", str(bare))
        remote = GitRemote(bare=bare, work=work)
        created.append(remote)
        return remote

    yield factory


@pytest.fixture
def fixture_docs() -> Path:
    return FIXTURE_DOCS
