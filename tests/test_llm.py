import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

import pytest

from specgraph.errors import ConfigError, IndexingError, LlmError, LlmRateLimitedError
from specgraph.llm import (
    RATE_LIMIT_MARKER,
    LlmCallCounter,
    _child_env,
    make_disabled_llm_func,
    make_llm_func,
    strip_think,
)
from specgraph.settings import load_settings


class FakeOllamaClient:
    def __init__(self, reply="답변"):
        self.calls = []
        self.reply = reply

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"role": "assistant", "content": self.reply}}


def _llm(settings, client, counter=None):
    counter = counter or LlmCallCounter()
    return make_llm_func(settings, counter, client_factory=lambda: client), counter


async def test_ollama_call_sends_think_false(settings):
    client = FakeOllamaClient()
    llm, _ = _llm(settings, client)

    result = await llm(
        "질문", system_prompt="시스템", history_messages=[{"role": "user", "content": "앞"}]
    )

    assert result == "답변"
    call = client.calls[0]
    assert call["think"] is False
    assert call["model"] == settings.llm_model
    assert call["messages"] == [
        {"role": "system", "content": "시스템"},
        {"role": "user", "content": "앞"},
        {"role": "user", "content": "질문"},
    ]
    assert call["stream"] is False


async def test_lightrag_internal_kwargs_are_not_forwarded(settings):
    client = FakeOllamaClient()
    llm, _ = _llm(settings, client)

    await llm("p", hashing_kv=object(), keyword_extraction=True, max_tokens=99, enable_cot=True)

    call = client.calls[0]
    assert "hashing_kv" not in call and "enable_cot" not in call
    assert call["format"] == "json"
    assert call["options"]["num_predict"] == 99


async def test_response_format_json_object_maps_to_format_json(settings):
    client = FakeOllamaClient()
    llm, _ = _llm(settings, client)

    await llm("p", response_format={"type": "json_object"})

    assert client.calls[0]["format"] == "json"


def test_strips_think_tags():
    assert strip_think("<think>추론</think>\n\n답") == "답"
    assert strip_think("앞 <think>a\nb</think> 뒤") == "앞 뒤"
    assert strip_think("추론 누락</think>답") == "답"
    assert strip_think("그냥 답") == "그냥 답"


async def test_think_tags_removed_from_reply(settings):
    llm, _ = _llm(settings, FakeOllamaClient("<think>생각</think>결론"))

    assert await llm("p") == "결론"


async def test_call_counter_attributes_to_current_doc_id(settings):
    llm, counter = _llm(settings, FakeOllamaClient())

    with counter.attribute_to("draft/a:prd/a.md#1"):
        await llm("p")
        await llm("p")
    await llm("p")

    assert counter.count_for("draft/a:prd/a.md#1") == 2
    assert counter.count_for("draft/a:prd/a.md#2") == 0
    assert counter.unattributed == 1
    assert counter.total == 3


async def test_attribution_propagates_to_child_tasks(settings):
    llm, counter = _llm(settings, FakeOllamaClient())

    with counter.attribute_to("d#1"):
        await asyncio.gather(llm("a"), llm("b"))

    assert counter.count_for("d#1") == 2


async def test_counter_reset(settings):
    llm, counter = _llm(settings, FakeOllamaClient())
    with counter.attribute_to("d#1"):
        await llm("a")

    counter.reset()

    assert counter.total == 0 and counter.count_for("d#1") == 0


async def test_chat_request_includes_configured_num_ctx(settings):
    client = FakeOllamaClient()
    llm, _ = _llm(settings, client)

    await llm("p")

    assert client.calls[0]["options"]["num_ctx"] == settings.llm_num_ctx


async def test_caller_options_num_ctx_overrides_setting(settings):
    client = FakeOllamaClient()
    llm, _ = _llm(settings, client)

    await llm("p", options={"num_ctx": 2048})

    assert client.calls[0]["options"]["num_ctx"] == 2048


# --- claude_code 바인딩 (PPS-350 AC2~AC7 · AC9) — runner 콜러블 주입 -----------------------------


def _result_json(result="답변", **extra):
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": result}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeRunner:
    """``runner(argv, stdin) -> (exit, stdout, stderr)`` 를 흉내 내고 호출을 기록한다."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [(0, _result_json(), b"")])

    async def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _claude(settings_env, runner=None, counter=None):
    settings_env.setenv("LLM_BINDING", "claude_code")
    settings = load_settings()
    counter = counter or LlmCallCounter()
    runner = runner or FakeRunner()
    return make_llm_func(settings, counter, runner=runner), runner, counter, settings


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


async def test_claude_code_binding_runs_claude_print_with_json_output_and_no_tools(settings_env):
    """D1: 구독 로그인(OAuth)을 끊는 ``--bare`` 는 쓰지 않고, 개별 플래그로 격리한다."""
    llm, runner, _, settings = _claude(settings_env)

    await llm("추출 프롬프트", system_prompt="시스템 지시")

    argv, stdin = runner.calls[0]
    assert argv[0] == "claude" and "-p" in argv
    assert _flag_value(argv, "--output-format") == "json"
    assert _flag_value(argv, "--model") == settings.llm_model
    assert _flag_value(argv, "--tools") == ""
    assert _flag_value(argv, "--system-prompt") == "시스템 지시"
    assert _flag_value(argv, "--setting-sources") == ""
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert "--bare" not in argv
    assert "추출 프롬프트" not in " ".join(argv)
    assert stdin.decode("utf-8") == "추출 프롬프트"


async def test_claude_code_returns_result_field_only(settings_env):
    runner = FakeRunner([(0, _result_json("본문만", session_id="s-1", total_cost_usd=0.1), b"")])
    llm, _, _, _ = _claude(settings_env, runner)

    assert await llm("p") == "본문만"


async def test_claude_code_history_messages_are_flattened_into_stdin_prompt(settings_env):
    """gleaning 의 history_messages 는 claude -p 가 받지 못하므로 단일 프롬프트로 직렬화한다."""
    llm, runner, _, _ = _claude(settings_env)
    history = [
        {"role": "user", "content": "첫 추출 요청"},
        {"role": "assistant", "content": "첫 추출 결과"},
    ]

    await llm("MANY entities were missed", system_prompt="s", history_messages=history)

    argv, stdin = runner.calls[0]
    text = stdin.decode("utf-8")
    assert text.index("첫 추출 요청") < text.index("첫 추출 결과") < text.index("MANY entities")
    assert text.rstrip().endswith("MANY entities were missed")
    assert "첫 추출 결과" not in " ".join(argv)


async def test_claude_code_strips_code_fence_when_json_requested(settings_env):
    fenced = '```json\n{"high_level_keywords": ["정산"]}\n```'
    llm, _, _, _ = _claude(settings_env, FakeRunner([(0, _result_json(fenced), b"")]))

    result = await llm("p", keyword_extraction=True)

    assert json.loads(result) == {"high_level_keywords": ["정산"]}


async def test_claude_code_nonzero_exit_raises_llm_error(settings_env):
    stdout = b'{"type":"result","subtype":"success","is_error":false,"result":"secret body"}'
    llm, _, _, _ = _claude(settings_env, FakeRunner([(1, stdout, b"Error: something broke")]))

    with pytest.raises(LlmError) as exc:
        await llm("p")

    assert isinstance(exc.value, IndexingError)
    assert not isinstance(exc.value, LlmRateLimitedError)
    assert "exit 1" in str(exc.value)
    assert "something broke" in str(exc.value)
    assert "secret body" not in str(exc.value)


async def test_claude_code_is_error_result_raises_llm_error(settings_env):
    stdout = _result_json("", subtype="error_during_execution", is_error=True)
    llm, _, _, _ = _claude(settings_env, FakeRunner([(0, stdout, b"")]))

    with pytest.raises(LlmError) as exc:
        await llm("p")

    assert not isinstance(exc.value, LlmRateLimitedError)
    assert "error_during_execution" in str(exc.value)


async def test_claude_code_invalid_json_output_raises_llm_error(settings_env):
    llm, _, _, _ = _claude(settings_env, FakeRunner([(0, b"not json at all", b"")]))

    with pytest.raises(LlmError):
        await llm("p")


async def test_claude_code_rate_limit_status_429_raises_rate_limited_without_retry_and_logs_event(
    settings_env, caplog
):
    """AC5: 한도 초과는 재시도하지 않고 event=llm_rate_limited 를 남긴다. 세션 ID 는 로그에 없다."""
    caplog.set_level(logging.ERROR)
    stdout = _result_json(
        "",
        subtype="error_during_execution",
        is_error=True,
        api_error_status=429,
        session_id="sess-secret",
    )
    runner = FakeRunner([(1, stdout, b"")])
    llm, _, _, _ = _claude(settings_env, runner)

    with pytest.raises(LlmRateLimitedError) as exc:
        await llm("p")

    assert len(runner.calls) == 1
    assert str(exc.value).startswith(RATE_LIMIT_MARKER)
    events = [r.getMessage() for r in caplog.records if "event=llm_rate_limited" in r.getMessage()]
    assert len(events) == 1
    assert "binding=claude_code" in events[0] and "api_error_status=429" in events[0]
    assert "sess-secret" not in events[0] and "sess-secret" not in str(exc.value)


async def test_claude_code_rate_limit_message_without_status_is_detected(settings_env):
    stdout = _result_json("You've hit your limit · resets 3pm", is_error=True, subtype="error")
    llm, _, _, _ = _claude(settings_env, FakeRunner([(0, stdout, b"")]))

    with pytest.raises(LlmRateLimitedError):
        await llm("p")


async def test_claude_code_usage_limit_reached_in_result_only_is_detected(settings_env):
    """R1: 공백형 'usage limit reached' 가 result 본문에만 있어도 한도로 판정한다."""
    stdout = _result_json(
        "Claude AI usage limit reached|1760000000", is_error=True, subtype="error"
    )
    llm, _, _, _ = _claude(settings_env, FakeRunner([(1, stdout, b"")]))

    with pytest.raises(LlmRateLimitedError):
        await llm("p")


async def test_claude_code_plain_text_stdout_usage_limit_is_detected(settings_env):
    """R1: stdout 이 JSON 이 아닌 평문 안내문이면 그 평문도 한도 문구로 검사한다."""
    stdout = b"Claude AI usage limit reached|1760000000\n"
    llm, _, _, _ = _claude(settings_env, FakeRunner([(1, stdout, b"")]))

    with pytest.raises(LlmRateLimitedError):
        await llm("p")


async def test_claude_code_bare_429_in_stderr_is_not_rate_limit(settings_env):
    """R1: stderr · result 의 숫자 429(토큰 수 · 줄 번호)는 한도가 아니다 — 일반 LlmError."""
    runner = FakeRunner([(1, b"", b"Error: HTTP 500 after 429 tokens")])
    llm, _, _, _ = _claude(settings_env, runner)

    with pytest.raises(LlmError) as exc:
        await llm("p")

    assert not isinstance(exc.value, LlmRateLimitedError)
    assert "exit 1" in str(exc.value)


async def test_claude_code_counter_records_each_call_including_failures(settings_env):
    """AC6: 실패한 호출도 센다(Ollama 경로와 같은 규약)."""
    runner = FakeRunner([(0, _result_json(), b""), (2, b"", b"boom"), (0, _result_json(), b"")])
    llm, _, counter, _ = _claude(settings_env, runner)

    with counter.attribute_to("d#1"):
        await llm("a")
        with pytest.raises(LlmError):
            await llm("b")
    await llm("c")

    assert counter.count_for("d#1") == 2 and counter.total == 3


async def test_claude_code_ignores_binding_host_and_num_ctx(settings_env):
    """AC7: HOST · NUM_CTX 가 없어도 동작하고 argv · stdin 어디에도 들어가지 않는다."""
    llm, runner, _, settings = _claude(settings_env)

    await llm("p", options={"num_ctx": 2048}, max_tokens=50)

    argv, stdin = runner.calls[0]
    joined = " ".join(argv) + stdin.decode("utf-8")
    assert settings.llm_binding_host not in joined
    assert "num_ctx" not in joined and "2048" not in joined


def test_claude_code_child_env_drops_api_billing_and_nested_session_vars():
    """D2: API 과금 경로 · 중첩 세션 표식은 자식 env 에서 제거한다(구독 경로만 탄다)."""
    env = {
        "ANTHROPIC_API_KEY": "x",
        "ANTHROPIC_AUTH_TOKEN": "y",
        "ANTHROPIC_BASE_URL": "https://gateway.example",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Org: z",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "AWS_BEARER_TOKEN_BEDROCK": "b",
        "CLAUDECODE": "1",
        "SPECGRAPH_GIT_TOKEN": "git-secret",
        "POSTGRES_PASSWORD": "pg-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "operator-own-token",
        "HOME": "/h",
        "PATH": "/usr/bin",
    }

    child = _child_env(env)

    # ANTHROPIC_* · CLAUDE_CODE_USE_* · SPECGRAPH_* · POSTGRES_* 접두어, Bedrock 토큰, 중첩 표식
    for dropped in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDECODE",
        "SPECGRAPH_GIT_TOKEN",
        "POSTGRES_PASSWORD",
    ):
        assert dropped not in child
    # 구독 로그인 토큰(운영자 본인)은 자식이 읽어야 하므로 남긴다.
    assert child == {
        "CLAUDE_CODE_OAUTH_TOKEN": "operator-own-token",
        "HOME": "/h",
        "PATH": "/usr/bin",
    }


@pytest.mark.parametrize("binding", ["ollama", "claude_code"])
def test_missing_llm_model_raises_config_error_when_building_llm_func(settings_env, binding):
    """D7: LLM_MODEL 은 Settings 가 아니라 LLM 함수를 만들 때 두 바인딩 모두가 요구한다."""
    settings_env.setenv("LLM_BINDING", binding)
    settings_env.delenv("LLM_MODEL")
    settings = load_settings()

    with pytest.raises(ConfigError, match="LLM_MODEL"):
        make_llm_func(settings, LlmCallCounter(), client_factory=lambda: None, runner=FakeRunner())


async def test_disabled_llm_func_records_call_and_raises_llm_error():
    """D8: MCP 의 LightRAG 초기화용 스텁 — 질의 경로에서 호출되면 테스트가 바로 깨진다."""
    counter = LlmCallCounter()
    llm = make_disabled_llm_func(counter)

    with pytest.raises(LlmError):
        await llm("p", system_prompt="s")

    assert counter.total == 1


async def test_default_binding_still_uses_ollama_client(settings_env):
    """AC8: LLM_BINDING 미설정이면 기존 Ollama 경로 그대로(client_factory 주입)."""
    client = FakeOllamaClient("올라마")
    runner = FakeRunner()
    settings = load_settings()

    llm = make_llm_func(settings, LlmCallCounter(), client_factory=lambda: client, runner=runner)

    assert settings.llm_binding == "ollama"
    assert await llm("p") == "올라마"
    assert client.calls and runner.calls == []


# --- claude_code 바인딩 — PATH 의 가짜 claude 스크립트(실제 서브프로세스, CQ3-b) ------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_claude(tmp_path, monkeypatch, body: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return script


def _real_claude_llm(settings_env, timeout: str):
    settings_env.setenv("LLM_BINDING", "claude_code")
    settings_env.setenv("LLM_TIMEOUT", timeout)
    return make_llm_func(load_settings(), LlmCallCounter())


async def test_claude_code_real_subprocess_runs_outside_repo_cwd_with_filtered_env(
    settings_env, tmp_path, monkeypatch
):
    """D1 · D2 실측: cwd 는 저장소 · 테스트 cwd 밖의 임시 디렉터리, API 키는 자식에 없다."""
    probe = tmp_path / "probe.txt"
    _fake_claude(
        tmp_path,
        monkeypatch,
        "cat > /dev/null\n"
        f'printf "%s\\n%s\\n%s\\n" "$PWD" "${{ANTHROPIC_API_KEY:-unset}}" '
        f'"${{SPECGRAPH_GIT_TOKEN:-unset}}" > "{probe}"\n'
        "printf '%s' '"
        '{"type":"result","subtype":"success","is_error":false,"result":"ok"}\'',
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked-key")
    monkeypatch.setenv("SPECGRAPH_GIT_TOKEN", "leaked-git-token")
    llm = _real_claude_llm(settings_env, "30")

    result = await llm("p", system_prompt="s")

    assert result == "ok"
    cwd, api_key, git_token = probe.read_text(encoding="utf-8").splitlines()
    child_cwd = Path(cwd)
    assert child_cwd != REPO_ROOT and REPO_ROOT not in child_cwd.parents
    assert child_cwd != Path.cwd()
    assert not await asyncio.to_thread(os.path.exists, cwd)  # 호출이 끝나면 임시 cwd 는 지워진다
    assert api_key == "unset"
    assert git_token == "unset"


async def test_hung_claude_process_times_out_with_llm_error_and_is_killed(
    settings_env, tmp_path, monkeypatch
):
    """AC4 · D10: LLM_TIMEOUT 안에 끝나지 않으면 손자 프로세스까지 그룹째 죽이고 LlmError."""
    import time

    pid_file = tmp_path / "grandchild.pid"
    _fake_claude(tmp_path, monkeypatch, f"sleep 30 &\necho $! > {pid_file}\nwait")
    llm = _real_claude_llm(settings_env, "1")

    with _kill_grandchild_on_exit(pid_file):
        started = time.monotonic()
        with pytest.raises(LlmError) as exc:
            await llm("p")

        assert time.monotonic() - started < 10
        assert "시간 초과" in str(exc.value) and "1s" in str(exc.value)
        assert not isinstance(exc.value, LlmRateLimitedError)
        grandchild = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 2
        while _is_alive(grandchild):
            if time.monotonic() >= deadline:
                pytest.fail("손자 프로세스가 살아 있다 — 프로세스 그룹째 죽이지 않았다")
            await asyncio.sleep(0.05)


async def test_cancelled_claude_call_kills_process_group(settings_env, tmp_path, monkeypatch):
    """종료 신호(poll 취소)가 claude 서브프로세스까지 전파된다(PPS-349 취소 처리와 연결)."""
    import time

    pid_file = tmp_path / "grandchild.pid"
    _fake_claude(tmp_path, monkeypatch, f"sleep 30 &\necho $! > {pid_file}\nwait")
    llm = _real_claude_llm(settings_env, "60")  # 시간 초과가 아니라 취소로만 끝나게 한다

    with _kill_grandchild_on_exit(pid_file):
        task = asyncio.create_task(llm("p"))
        deadline = time.monotonic() + 5
        while not (pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()):
            if time.monotonic() >= deadline:
                task.cancel()
                pytest.fail("가짜 claude 가 손자를 만들지 못했다")
            await asyncio.sleep(0.02)
        grandchild = int(pid_file.read_text(encoding="utf-8").strip())
        assert grandchild > 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

        deadline = time.monotonic() + 3
        while _is_alive(grandchild):
            if time.monotonic() >= deadline:
                pytest.fail("취소 후에도 손자 프로세스가 살아 있다 — 프로세스 그룹을 죽이지 않았다")
            await asyncio.sleep(0.05)


def _is_alive(pid: int) -> bool:
    """살아 있는 프로세스인가. 좀비는 종료로 본다(``ps`` 로 상태를 읽는다 — macOS 에 /proc 없음)."""
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
    """블록이 어떻게 끝나든 pid 파일의 프로세스가 살아 있으면 SIGKILL 한다."""
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
