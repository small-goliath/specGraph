import asyncio
import contextlib
import logging

import pytest
from pydantic import SecretStr

from specgraph.errors import GitSourceError, NonUtf8FileError
from specgraph.indexer.git_source import GitSource, mask_secret


def _source(remote, tmp_path, token=None):
    return GitSource(
        repo_url=remote.url,
        cache_dir=tmp_path / "mirror",
        token=SecretStr(token) if token else None,
        branch_glob="draft/*",
    )


async def test_hung_remote_git_times_out_with_git_source_error(tmp_path, monkeypatch):
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    source = GitSource(
        repo_url="https://example.invalid/product-docs.git",
        cache_dir=tmp_path / "mirror",
        token=None,
        timeout_seconds=0.5,
    )

    started = time.monotonic()
    with pytest.raises(GitSourceError) as exc:
        await source.list_branches()

    assert time.monotonic() - started < 5
    assert "시간 초과" in str(exc.value) and "ls-remote" in str(exc.value)


async def test_hung_git_with_grandchild_holding_pipes_is_killed_as_group(tmp_path, monkeypatch):
    """git-remote-https 처럼 손자 프로세스가 stdout/stderr 파이프를 쥔 경우도 제때 끝난다."""
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    fake_git = bin_dir / "git"
    fake_git.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    source = GitSource(
        repo_url="https://example.invalid/product-docs.git",
        cache_dir=tmp_path / "mirror",
        token=None,
        timeout_seconds=0.5,
    )

    with _kill_grandchild_on_exit(pid_file):  # 단언이 실패해도 손자(sleep 30)를 남기지 않는다
        started = time.monotonic()
        with pytest.raises(GitSourceError):
            await source.list_branches()

        assert time.monotonic() - started < 4
        grandchild = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 2
        while _is_alive(grandchild):
            if time.monotonic() >= deadline:
                pytest.fail("손자 프로세스가 살아 있다 — 프로세스 그룹째 죽이지 않았다")
            await asyncio.sleep(0.05)


async def test_cancelled_git_call_kills_process_group(tmp_path, monkeypatch):
    """취소(SIGTERM 종료 경로)돼도 git 과 그 손자 프로세스를 그룹째 정리한다."""
    import time

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    fake_git = bin_dir / "git"
    fake_git.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    source = GitSource(
        repo_url="https://example.invalid/product-docs.git",
        cache_dir=tmp_path / "mirror",
        token=None,
        timeout_seconds=60,  # 시간 초과가 아니라 취소로만 끝나게 한다
    )

    with _kill_grandchild_on_exit(pid_file):
        task = asyncio.create_task(source.list_branches())
        deadline = time.monotonic() + 5
        while not (pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()):
            if time.monotonic() >= deadline:
                task.cancel()
                pytest.fail("가짜 git 이 손자를 만들지 못했다")
            await asyncio.sleep(0.02)
        grandchild = int(pid_file.read_text(encoding="utf-8").strip())
        assert grandchild > 1  # kill 대상 pid 가드

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        deadline = time.monotonic() + 3
        while _is_alive(grandchild):
            if time.monotonic() >= deadline:
                pytest.fail("취소 후에도 손자 프로세스가 살아 있다 — 프로세스 그룹을 죽이지 않았다")
            await asyncio.sleep(0.05)


def _is_alive(pid: int) -> bool:
    """살아 있는 프로세스인가. 좀비(``Z``, 아직 reap 되지 않은 종료 프로세스)는 종료로 본다.

    reaper 가 없는 컨테이너나 macOS 에서는 죽은 손자가 좀비로 남아 ``os.kill(pid, 0)`` 이 계속
    성공한다. ``/proc`` 은 macOS 에 없으므로 ``ps`` 로 상태를 읽는다.
    """
    import os
    import subprocess

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


@contextlib.contextmanager
def _kill_grandchild_on_exit(pid_file):
    """블록이 어떻게 끝나든(단언 실패 포함) pid 파일의 프로세스가 살아 있으면 SIGKILL 한다.

    pid 파일이 아직 없거나(손자가 만들어지기 전 실패) 비어 있어도 안전하다.
    """
    import os
    import signal

    try:
        yield
    finally:
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            pid = None
        if pid is not None and _is_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_is_alive_treats_zombie_as_dead():
    import subprocess
    import time

    child = subprocess.Popen(["sleep", "0"])  # wait() 하지 않는다 → 종료 뒤 좀비로 남는다
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child.pid)], capture_output=True, text=True
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("좀비를 만들지 못했다")

        assert _is_alive(child.pid) is False
    finally:
        child.wait()


def test_is_alive_true_for_running_process_and_false_after_reap():
    import subprocess

    child = subprocess.Popen(["sleep", "30"])
    try:
        assert _is_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()
    assert _is_alive(child.pid) is False


def test_grandchild_cleanup_runs_even_when_assertion_fails(tmp_path):
    import subprocess

    child = subprocess.Popen(["sleep", "30"])
    pid_file = tmp_path / "grandchild.pid"
    pid_file.write_text(f"{child.pid}\n", encoding="utf-8")

    with pytest.raises(AssertionError):
        with _kill_grandchild_on_exit(pid_file):
            raise AssertionError("검증 실패")

    assert child.wait(timeout=5) == -9  # SIGKILL 로 정리됐다


def test_grandchild_cleanup_tolerates_missing_pid_file(tmp_path):
    with _kill_grandchild_on_exit(tmp_path / "never-created.pid"):
        pass


async def test_remote_git_calls_set_low_speed_abort(git_remote, tmp_path):
    remote = git_remote()
    sha = remote.commit("draft/a", {"prd/a.md": "a"})
    source = GitSource(
        repo_url=remote.url, cache_dir=tmp_path / "mirror", token=None, timeout_seconds=30
    )

    await source.list_branches()
    await source.fetch("draft/a", sha)

    remote_calls = [argv for argv in source.command_log if {"ls-remote", "fetch"} & set(argv)]
    assert len(remote_calls) == 2
    for argv in remote_calls:
        assert "http.lowSpeedLimit=1" in argv
        assert "http.lowSpeedTime=30" in argv


async def test_non_utf8_file_raises_non_utf8_error_with_path(git_remote, tmp_path):
    remote = git_remote()
    sha = remote.commit("draft/a", {"prd/bad.md": "정산".encode("cp949")})
    source = _source(remote, tmp_path)
    await source.fetch("draft/a", sha)

    with pytest.raises(NonUtf8FileError) as exc:
        await source.read_file(sha, "prd/bad.md")

    assert isinstance(exc.value, GitSourceError)
    assert "prd/bad.md" in str(exc.value)


async def test_lists_only_draft_branches_with_head_sha(git_remote, tmp_path):
    remote = git_remote()
    a = remote.commit("draft/settlr-a", {"prd/a.md": "# a"})
    b = remote.commit("draft/settlr-b", {"prd/b.md": "# b"})
    remote.commit("main", {"README.md": "main"})
    remote.commit("feature/draft-x", {"x.md": "x"})

    branches = await _source(remote, tmp_path).list_branches()

    assert branches == {"draft/settlr-a": a, "draft/settlr-b": b}


async def test_new_branch_visible_without_config_change(git_remote, tmp_path):
    remote = git_remote()
    remote.commit("draft/one", {"prd/1.md": "1"})
    source = _source(remote, tmp_path)
    assert set(await source.list_branches()) == {"draft/one"}

    new_sha = remote.commit("draft/two", {"prd/2.md": "2"})

    assert (await source.list_branches())["draft/two"] == new_sha


async def test_deleted_branch_disappears(git_remote, tmp_path):
    remote = git_remote()
    remote.commit("draft/one", {"prd/1.md": "1"})
    remote.commit("draft/two", {"prd/2.md": "2"})
    source = _source(remote, tmp_path)

    remote.delete_branch("draft/two")

    assert set(await source.list_branches()) == {"draft/one"}


async def test_empty_remote_returns_no_branches(git_remote, tmp_path):
    assert await _source(git_remote(), tmp_path).list_branches() == {}


async def test_reads_files_at_commit(git_remote, tmp_path):
    remote = git_remote()
    first = remote.commit("draft/a", {"prd/a.md": "첫 버전", "README.md": "r"})
    second = remote.commit("draft/a", {"prd/a.md": "둘째 버전", "qa/q.md": "q"})
    source = _source(remote, tmp_path)

    await source.fetch("draft/a", second)

    assert sorted(await source.list_files(second)) == ["README.md", "prd/a.md", "qa/q.md"]
    assert await source.read_file(second, "prd/a.md") == "둘째 버전"
    assert await source.read_file(first, "prd/a.md") == "첫 버전"


async def test_fetch_unknown_branch_raises_git_source_error(git_remote, tmp_path):
    remote = git_remote()
    remote.commit("draft/a", {"prd/a.md": "a"})

    with pytest.raises(GitSourceError):
        await _source(remote, tmp_path).fetch("draft/missing", "0" * 40)


async def test_unreachable_remote_raises_git_source_error(tmp_path):
    source = GitSource(
        repo_url=(tmp_path / "nope.git").as_uri(), cache_dir=tmp_path / "m", token=None
    )

    with pytest.raises(GitSourceError):
        await source.list_branches()


async def test_token_never_appears_in_command_or_log(git_remote, tmp_path, caplog):
    token = "ghp_TOPSECRET123"
    remote = git_remote()
    sha = remote.commit("draft/a", {"prd/a.md": "a"})
    source = _source(remote, tmp_path, token=token)
    caplog.set_level(logging.DEBUG)

    await source.list_branches()
    await source.fetch("draft/a", sha)
    for argv in source.command_log:
        assert token not in " ".join(argv)
    assert token not in caplog.text
    assert token not in source.repo_url


async def test_error_message_masks_token(tmp_path):
    token = "ghp_TOPSECRET123"
    source = GitSource(
        repo_url=f"https://x:{token}@127.0.0.1:1/none.git",
        cache_dir=tmp_path / "m",
        token=SecretStr(token),
    )

    with pytest.raises(GitSourceError) as exc:
        await source.list_branches()

    assert token not in str(exc.value)


def test_mask_secret():
    assert mask_secret("url https://a:tok@h and tok", "tok") == "url https://a:***@h and ***"
    assert mask_secret("nothing", None) == "nothing"
