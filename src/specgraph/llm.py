"""LLM 함수 팩토리 · 호출 카운터 (AC1 · AC2 · AC9 · PPS-350).

LightRAG 타입(``EmbeddingFunc``)이나 LightRAG 바인딩을 쓰는 임베딩 · 리랭커 팩토리는
``lightrag_store.py`` 에 있다(LightRAG API 차이는 그 파일 한 곳에서만 흡수한다).

- 모델명 · 엔드포인트는 Settings(환경변수)에서만 온다. 코드에 모델 리터럴이 없다.
- ``LLM_BINDING`` 으로 경로를 고른다(PPS-350).
  - ``ollama``(기본): Ollama chat 을 ``think=False``(non-thinking)로 부르고, 응답에 남은
    ``<think>`` 블록을 지운다.
  - ``claude_code``: 구독 로그인된 Claude Code 의 헤드리스 모드(``claude -p``)를 서브프로세스로
    띄운다(D1). 프롬프트는 stdin, cwd 는 저장소 밖 임시 디렉터리, ``--bare`` 금지(OAuth 를 끊는다).
    자식 env 에서 API 과금 변수(``ANTHROPIC_API_KEY`` 등)와 중첩 세션 표식을 제거한다(D2).
    결과 JSON 은 ``type=result · subtype=success · is_error=false`` 일 때만 성공(D3). 한도 초과는
    ``LlmRateLimitedError`` 로 재시도 없이 올리고 ``event=llm_rate_limited`` 를 남긴다(AC5).
    LightRAG 가 삼킨 예외 메시지에서 타입을 복원할 수 있게 메시지 앞에 ``RATE_LIMIT_MARKER`` 를
    둔다(D5).
- ``LLM_MODEL`` 은 두 바인딩 모두 ``make_llm_func`` 가 요구한다(D7). MCP 는
  ``make_disabled_llm_func`` 로 LightRAG 를 초기화하므로 LLM 설정이 필요 없다(D8).
- ``LlmCallCounter`` 가 모든 호출(실패 포함)을 세고, ``contextvars`` 로 현재 doc_id 에 귀속한다.
  변경 없는 챕터의 ``llm_calls=0`` 로그가 이 카운터에서 나온다.
- 로그 · 예외 메시지에 세션 ID · 응답 JSON 전체 · 프롬프트를 넣지 않는다(제약).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import signal
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import Any

from specgraph.errors import ConfigError, LlmError, LlmRateLimitedError
from specgraph.log import log_event
from specgraph.settings import Settings

logger = logging.getLogger(__name__)

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_UNOPENED_THINK = re.compile(r"^.*?</think>\s*", re.DOTALL)
_CODE_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```\s*$", re.DOTALL)
# 한도 초과 식별 문구(D3). 넓게 잡지 않는다 — 실패는 넓게, 한도만 좁게.
# 텍스트 패턴은 result · subtype · api_error_code · stderr 와 (JSON 이 아닐 때) stdout 평문에
# 적용한다. bare 429 는 ``api_error_status``(정수) · ``api_error_code``(문자열)에서만 본다 —
# stderr · result 의 숫자 429(토큰 수 · 줄 번호)로 한도를 판정하지 않는다.
_RATE_LIMIT_PATTERN = re.compile(
    r"rate[ _]?limit|usage[ _]?limit|limit reached|hit your (usage )?limit", re.I
)
_HTTP_429 = re.compile(r"\b429\b")

# LightRAG 가 llm_model_func 에 넘기지만 Ollama chat() · claude -p 는 받지 않는 인자들.
_DROPPED_KWARGS = (
    "hashing_kv",
    "enable_cot",
    "token_tracker",
    "stream",
    "keyword_extraction",
    "entity_extraction",
    "response_format",
    "max_tokens",
    "image_inputs",
    "timeout",
    "host",
    "api_key",
)

# 자식 claude 프로세스에 넘기지 않는 변수(D2) — API 과금 · 게이트웨이 경로(ANTHROPIC_* 전부 ·
# CLAUDE_CODE_USE_* · Bedrock 토큰), 중첩 세션 표식, 인덱서 비밀(SPECGRAPH_* · POSTGRES_*).
# ``CLAUDE_CODE_OAUTH_TOKEN`` 은 구독 로그인 토큰이라 남긴다 — 운영자 본인 토큰이어야 한다.
_CHILD_ENV_DROPPED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "SPECGRAPH_", "POSTGRES_")
_CHILD_ENV_DROPPED = ("AWS_BEARER_TOKEN_BEDROCK", "CLAUDECODE")
_CLAUDE_BIN = "claude"
# 타임아웃 · 취소 뒤 프로세스 그룹을 죽이고 종료를 기다리는 상한(초). git_source 와 같은 값.
_KILL_WAIT_SECONDS = 5.0
_STDERR_SNIPPET_CHARS = 300
# LlmRateLimitedError 메시지 접두 — LightRAG 가 str(exc)[:500] 으로 삼킨 뒤 lightrag_store 가
# 타입을 복원한다(D5).
RATE_LIMIT_MARKER = "llm_rate_limited"

LlmFunc = Callable[..., Awaitable[str]]
# claude -p 실행기: (argv, stdin) → (exit code, stdout, stderr). 테스트는 가짜를 주입한다(CQ3-a).
Runner = Callable[[Sequence[str], bytes], Awaitable[tuple[int, bytes, bytes]]]


def strip_think(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = _UNOPENED_THINK.sub("", text)
    return text.strip()


class LlmCallCounter:
    """LLM 호출 수를 doc_id 별로 센다. ``attribute_to`` 안에서 생긴 하위 태스크도 귀속된다."""

    def __init__(self) -> None:
        self._current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"specgraph_llm_doc_{id(self)}", default=None
        )
        self._counts: Counter[str | None] = Counter()

    @contextlib.contextmanager
    def attribute_to(self, doc_id: str) -> Iterator[None]:
        token = self._current.set(doc_id)
        try:
            yield
        finally:
            self._current.reset(token)

    def record(self) -> None:
        self._counts[self._current.get()] += 1

    def count_for(self, doc_id: str) -> int:
        return self._counts.get(doc_id, 0)

    @property
    def unattributed(self) -> int:
        return self._counts.get(None, 0)

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def reset(self) -> None:
        self._counts.clear()


def _default_client_factory(settings: Settings) -> Callable[[], Any]:
    def factory() -> Any:
        import ollama

        return ollama.AsyncClient(host=settings.llm_binding_host, timeout=settings.llm_timeout)

    return factory


def _wants_json(kwargs: dict[str, Any]) -> bool:
    fmt = kwargs.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
        return True
    return bool(kwargs.get("keyword_extraction") or kwargs.get("entity_extraction"))


def _reply_text(response: Any) -> str:
    if isinstance(response, dict):
        return str(response["message"]["content"] or "")
    return str(response.message.content or "")


def _require_model(settings: Settings) -> str:
    if not settings.llm_model:
        raise ConfigError(f"LLM_MODEL 이 필요하다(LLM_BINDING={settings.llm_binding})")
    return settings.llm_model


def make_llm_func(
    settings: Settings,
    counter: LlmCallCounter,
    client_factory: Callable[[], Any] | None = None,
    runner: Runner | None = None,
) -> LlmFunc:
    """``LLM_BINDING`` 에 따라 Ollama 또는 ``claude -p`` 를 부르는 ``LlmFunc`` 를 만든다.

    ``client_factory`` 는 ollama 경로의 클라이언트 주입점, ``runner`` 는 claude_code 경로의
    서브프로세스 실행기 주입점이다(각각 테스트용). ``LLM_MODEL`` 이 없으면 ``ConfigError``.
    """
    model = _require_model(settings)
    if settings.llm_binding == "claude_code":
        return _claude_code_llm_func(settings, model, counter, runner or _default_runner(settings))
    return _ollama_llm_func(
        settings, model, counter, client_factory or _default_client_factory(settings)
    )


def make_disabled_llm_func(counter: LlmCallCounter) -> LlmFunc:
    """LLM 을 쓰지 않는 프로세스(MCP 서버)의 LightRAG 초기화용 스텁(D8).

    LightRAG 는 ``llm_model_func=None`` 을 거부하므로 함수 객체는 넘겨야 한다. 호출되면 카운터에
    기록한 뒤 ``LlmError`` 를 낸다 — 질의 경로에서 LLM 이 불리면 테스트 · 운영 모두에서 바로
    드러난다.
    """

    async def disabled_llm_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        counter.record()
        raise LlmError(
            "LLM 이 비활성화된 프로세스에서 LLM 호출이 발생했다(MCP 질의 경로는 LLM 0 건)"
        )

    return disabled_llm_func


# --- ollama ------------------------------------------------------------------------


def _ollama_llm_func(
    settings: Settings, model: str, counter: LlmCallCounter, factory: Callable[[], Any]
) -> LlmFunc:
    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history_messages or [])
        messages.append({"role": "user", "content": prompt})

        options: dict[str, Any] = dict(kwargs.pop("options", None) or {})
        if kwargs.get("max_tokens") is not None:
            options.setdefault("num_predict", kwargs["max_tokens"])
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "think": False,
            "stream": False,
        }
        if _wants_json(kwargs):
            request["format"] = "json"
        options.setdefault("num_ctx", settings.llm_num_ctx)
        request["options"] = options
        for key in _DROPPED_KWARGS:
            kwargs.pop(key, None)

        counter.record()
        response = await factory().chat(**request)
        return strip_think(_reply_text(response))

    return llm_model_func


# --- claude_code -------------------------------------------------------------------


def _is_dropped_env(name: str) -> bool:
    return name in _CHILD_ENV_DROPPED or name.startswith(_CHILD_ENV_DROPPED_PREFIXES)


def _child_env(env: Mapping[str, str]) -> dict[str, str]:
    """자식 ``claude`` 프로세스의 env(D2).

    API 과금 · 게이트웨이 변수, 중첩 세션 표식, 인덱서 비밀을 뺀 사본.
    """
    return {k: v for k, v in env.items() if not _is_dropped_env(k)}


def _flatten_history(history: list[dict[str, Any]] | None, prompt: str) -> str:
    """``claude -p`` 는 메시지 배열을 받지 않으므로 history 를 하나의 프롬프트로 만든다."""
    if not history:
        return prompt
    parts = [f"[{m.get('role', 'user')}]\n{m.get('content', '')}" for m in history]
    parts.append(f"[user]\n{prompt}")
    return "\n\n".join(parts)


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE.match(text)
    return match.group(1).strip() if match else text.strip()


def _claude_argv(model: str, system_prompt: str | None) -> list[str]:
    argv = [
        _CLAUDE_BIN,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources",
        "",
    ]
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    return argv


def _parse_result(stdout: bytes) -> dict[str, Any] | None:
    """stdout 의 결과 객체(``type=result``). JSON 이 아니거나 형태가 다르면 None."""
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    if isinstance(payload, list):
        payload = next(
            (p for p in reversed(payload) if isinstance(p, dict) and p.get("type") == "result"),
            None,
        )
    return payload if isinstance(payload, dict) else None


def _looks_rate_limited(payload: dict[str, Any] | None, stdout: str, stderr: str) -> bool:
    """한도 초과 판정(D3 · R1).

    bare 429 는 ``api_error_status`` · ``api_error_code`` 에서만 인정한다. 텍스트 문구는 result ·
    subtype · api_error_code · stderr 에 적용하고, stdout 이 결과 JSON 이 아니면 그 평문에도
    적용한다.
    """
    if payload is not None:
        if payload.get("api_error_status") == 429:
            return True
        code = str(payload.get("api_error_code") or "")
        if _HTTP_429.search(code) or _RATE_LIMIT_PATTERN.search(code):
            return True
        for key in ("subtype", "result"):
            if _RATE_LIMIT_PATTERN.search(str(payload.get(key) or "")):
                return True
    elif _RATE_LIMIT_PATTERN.search(stdout):
        return True
    return bool(_RATE_LIMIT_PATTERN.search(stderr))


def _judge(returncode: int, stdout: bytes, stderr: bytes, model: str) -> str:
    """D3 판정 — 성공이면 ``result`` 본문, 아니면 ``LlmError``(한도 초과는 하위 타입)."""
    payload = _parse_result(stdout)
    if (
        returncode == 0
        and payload is not None
        and payload.get("type") == "result"
        and payload.get("subtype") == "success"
        and not payload.get("is_error")
    ):
        return str(payload.get("result") or "")

    err_text = stderr.decode("utf-8", "replace").strip()
    out_text = stdout.decode("utf-8", "replace") if payload is None else ""
    status = payload.get("api_error_status") if payload else None
    subtype = payload.get("subtype") if payload else None
    if _looks_rate_limited(payload, out_text, err_text):
        log_event(
            logger,
            "llm_rate_limited",
            logging.ERROR,
            binding="claude_code",
            model=model,
            exit_code=returncode,
            api_error_status=status,
            subtype=subtype,
        )
        raise LlmRateLimitedError(
            f"{RATE_LIMIT_MARKER}: claude -p 구독 사용량 한도 초과"
            f"(exit {returncode}, api_error_status={status}) — 재시도하지 않는다"
        )
    if returncode != 0:
        snippet = err_text[:_STDERR_SNIPPET_CHARS]
        raise LlmError(f"claude -p 실패(exit {returncode}): {snippet}")
    if payload is None:
        raise LlmError("claude -p 출력을 결과 JSON 으로 읽을 수 없다(exit 0)")
    raise LlmError(
        f"claude -p 오류 응답(subtype={subtype}, is_error={payload.get('is_error')},"
        f" api_error_status={status})"
    )


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """claude 와 그 자식을 프로세스 그룹째 죽이고, 상한을 두고 기다린다."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_KILL_WAIT_SECONDS)


def _default_runner(settings: Settings) -> Runner:
    """실제 ``claude`` 서브프로세스 실행기.

    저장소 밖 임시 cwd · 걸러낸 env(D2) · ``LLM_TIMEOUT`` 뒤 프로세스 그룹 kill(D10).
    """
    timeout = float(settings.llm_timeout)

    async def run(argv: Sequence[str], stdin: bytes) -> tuple[int, bytes, bytes]:
        with tempfile.TemporaryDirectory(prefix="specgraph-claude-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=cwd,
                    env=_child_env(os.environ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise LlmError(f"claude 실행 실패: {exc}") from None
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
            except TimeoutError:
                await _kill_group(proc)
                raise LlmError(
                    f"claude -p 시간 초과({settings.llm_timeout}s) — 프로세스 그룹을 종료했다"
                ) from None
            except asyncio.CancelledError:
                await _kill_group(proc)
                raise
            return proc.returncode or 0, out, err

    return run


def _claude_code_llm_func(
    settings: Settings, model: str, counter: LlmCallCounter, runner: Runner
) -> LlmFunc:
    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        wants_json = _wants_json(kwargs)
        kwargs.pop("options", None)  # Ollama 전용(num_ctx 등) — claude_code 는 읽지 않는다(AC7)
        for key in _DROPPED_KWARGS:
            kwargs.pop(key, None)
        argv = _claude_argv(model, system_prompt)
        stdin = _flatten_history(history_messages, prompt).encode("utf-8")
        log_event(
            logger,
            "llm_exec",
            logging.DEBUG,
            binding="claude_code",
            model=model,
            prompt_bytes=len(stdin),
            history=len(history_messages or ()),
        )

        counter.record()
        returncode, out, err = await runner(argv, stdin)
        result = _judge(returncode, out, err, model)
        return _strip_code_fence(result) if wants_json else result

    return llm_model_func


async def openai_compatible_embed(texts: list[str], model: str, base_url: str, **_: Any) -> Any:
    """OpenAI 호환 ``/embeddings`` (예: 로컬 Infinity 컨테이너).

    ``lightrag.llm.openai`` 는 import 시 openai 패키지를 런타임 설치하려 하므로 쓰지 않는다.
    """
    import httpx
    import numpy as np

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            base_url.rstrip("/") + "/embeddings", json={"model": model, "input": texts}
        )
        response.raise_for_status()
    data = sorted(response.json()["data"], key=lambda item: item["index"])
    return np.array([item["embedding"] for item in data], dtype=np.float32)
