"""product-docs git 원격 — 읽기 전용 접근 (AC4 · AC5 · AC6 · AC7).

사용하는 git 명령은 ``ls-remote`` · ``fetch`` · ``ls-tree`` · ``show`` 뿐이다(push 없음).
토큰은 URL · argv · 로그에 넣지 않고 ``GIT_ASKPASS`` 스크립트가 환경변수에서 읽어 전달한다.
브랜치는 원격에서 매번 탐색하므로 브랜치별 설정이 없다.
모든 git 호출은 ``timeout_seconds``(``SPECGRAPH_GIT_TIMEOUT_SECONDS``) 안에 끝나야 한다. 넘기면
프로세스 **그룹**째(git-remote-https · ssh 같은 손자 프로세스 포함) 죽이고 ``GitSourceError`` 를
낸다(네트워크가 멈춰도 데몬이 영구 정지하지 않는다). 원격 호출에는 보조로
``http.lowSpeedLimit/lowSpeedTime`` 을 걸어 git 스스로도 멈춘 전송을 끊게 한다.
UTF-8 이 아닌 파일은 ``NonUtf8FileError`` 로 알린다(호출자가 그 파일만 건너뛴다).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import os
import signal
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from specgraph.errors import GitSourceError, NonUtf8FileError
from specgraph.log import log_event

logger = logging.getLogger(__name__)

# 타임아웃 뒤 프로세스 그룹을 죽이고 종료를 기다리는 상한(초). 이 안에 안 끝나도 더 기다리지 않는다.
KILL_WAIT_SECONDS = 5.0


class GitSourcePort(Protocol):
    """인덱서가 쓰는 git 읽기 포트(읽기 전용)."""

    async def list_branches(self) -> dict[str, str]: ...

    async def fetch(self, branch: str, sha: str) -> None: ...

    async def list_files(self, sha: str) -> list[str]: ...

    async def read_file(self, sha: str, path: str) -> str: ...


_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "${SPECGRAPH_ASKPASS_USER:-x-access-token}" ;;
  *) printf '%s\\n' "$SPECGRAPH_ASKPASS_TOKEN" ;;
esac
"""
_REF_PREFIX = "refs/heads/"
_LOCAL_NS = "refs/specgraph/"


def mask_secret(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "***")


class GitSource:
    def __init__(
        self,
        repo_url: str,
        cache_dir: Path,
        token: SecretStr | None,
        branch_glob: str = "draft/*",
        timeout_seconds: float = 120,
    ) -> None:
        self.repo_url = repo_url
        self.timeout_seconds = timeout_seconds
        self.cache_dir = Path(cache_dir)
        self.branch_glob = branch_glob
        self._token = token.get_secret_value() if token else None
        self.command_log: list[list[str]] = []
        self._askpass: Path | None = None

    # --- 내부 -----------------------------------------------------------------

    def _mask(self, text: str) -> str:
        return mask_secret(text, self._token)

    def _askpass_path(self) -> Path:
        if self._askpass is None:
            directory = Path(tempfile.mkdtemp(prefix="specgraph-askpass-"))
            script = directory / "askpass.sh"
            script.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
            script.chmod(stat.S_IRWXU)
            self._askpass = script
        return self._askpass

    def _env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        if self._token:
            env["GIT_ASKPASS"] = str(self._askpass_path())
            env["SPECGRAPH_ASKPASS_TOKEN"] = self._token
        return env

    def _remote_options(self) -> list[str]:
        """원격 전송이 1 byte/s 미만으로 timeout 동안 이어지면 git 이 스스로 끊는다(보조 장치)."""
        low_speed_time = max(1, int(self.timeout_seconds))
        return ["-c", "http.lowSpeedLimit=1", "-c", f"http.lowSpeedTime={low_speed_time}"]

    @staticmethod
    async def _kill_group(proc: asyncio.subprocess.Process) -> None:
        """git 과 그 자식(git-remote-https 등)을 프로세스 그룹째 죽이고, 상한을 두고 기다린다."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_SECONDS)

    async def _git_bytes(self, *args: str, cwd: Path | None = None, remote: bool = False) -> bytes:
        options = self._remote_options() if remote else []
        argv = ["git", "-c", "credential.helper=", *options, *args]
        self.command_log.append(argv)
        log_event(logger, "git_exec", logging.DEBUG, argv=self._mask(" ".join(argv)))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=self._env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise GitSourceError(self._mask(f"git 실행 실패: {exc}")) from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except TimeoutError:
            await self._kill_group(proc)
            raise GitSourceError(
                f"git {args[0]} 시간 초과({self.timeout_seconds}s) — 프로세스 그룹을 종료했다"
            ) from None
        if proc.returncode != 0:
            message = err.decode("utf-8", "replace").strip()
            raise GitSourceError(
                self._mask(f"git {args[0]} 실패(exit {proc.returncode}): {message}")
            )
        return out

    async def _git(self, *args: str, cwd: Path | None = None, remote: bool = False) -> str:
        return (await self._git_bytes(*args, cwd=cwd, remote=remote)).decode("utf-8", "replace")

    async def _ensure_mirror(self) -> Path:
        if not (self.cache_dir / "HEAD").exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-q", "--bare", str(self.cache_dir))
        return self.cache_dir

    # --- 공개 API -------------------------------------------------------------

    async def list_branches(self) -> dict[str, str]:
        """원격의 ``branch_glob`` 브랜치 → HEAD SHA."""
        out = await self._git(
            "ls-remote", "--heads", self.repo_url, _REF_PREFIX + self.branch_glob, remote=True
        )
        branches: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if not ref.startswith(_REF_PREFIX):
                continue
            branch = ref[len(_REF_PREFIX) :]
            if fnmatch.fnmatchcase(branch, self.branch_glob):
                branches[branch] = sha.strip()
        return branches

    async def fetch(self, branch: str, sha: str) -> None:
        mirror = await self._ensure_mirror()
        await self._git(
            "fetch",
            "-q",
            "--no-tags",
            self.repo_url,
            f"+{_REF_PREFIX}{branch}:{_LOCAL_NS}{branch}",
            cwd=mirror,
            remote=True,
        )
        try:
            await self._git("cat-file", "-e", f"{sha}^{{commit}}", cwd=mirror)
        except GitSourceError:
            raise GitSourceError(f"{branch} 에서 커밋 {sha} 를 찾지 못했다") from None

    async def list_files(self, sha: str) -> list[str]:
        mirror = await self._ensure_mirror()
        out = await self._git("ls-tree", "-r", "--name-only", "-z", sha, cwd=mirror)
        return [p for p in out.split("\0") if p]

    async def read_file(self, sha: str, path: str) -> str:
        mirror = await self._ensure_mirror()
        raw = await self._git_bytes("show", f"{sha}:{path}", cwd=mirror)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NonUtf8FileError(f"{path} 는 UTF-8 이 아니다({exc.reason})") from None

    async def read_files(self, sha: str, paths: Sequence[str]) -> dict[str, str]:
        return {path: await self.read_file(sha, path) for path in paths}
