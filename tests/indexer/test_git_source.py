import asyncio
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
    import os
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

    started = time.monotonic()
    with pytest.raises(GitSourceError):
        await source.list_branches()

    assert time.monotonic() - started < 4
    grandchild = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(grandchild, 9)
        pytest.fail("손자 프로세스가 살아 있다 — 프로세스 그룹째 죽이지 않았다")


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
