"""LLM 함수 팩토리 · 호출 카운터 (AC1 · AC2 · AC9).

LightRAG 타입(``EmbeddingFunc``)이나 LightRAG 바인딩을 쓰는 임베딩 · 리랭커 팩토리는
``lightrag_store.py`` 에 있다(LightRAG API 차이는 그 파일 한 곳에서만 흡수한다).

- 모델명 · 엔드포인트는 Settings(환경변수)에서만 온다. 코드에 모델 리터럴이 없다.
- LLM 은 Ollama chat 을 ``think=False``(non-thinking)로 부르고, 응답에 남은 ``<think>`` 블록을
  지운다.
- ``LlmCallCounter`` 가 모든 호출을 세고, ``contextvars`` 로 현재 doc_id 에 귀속한다.
  변경 없는 챕터의 ``llm_calls=0`` 로그가 이 카운터에서 나온다.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from specgraph.settings import Settings

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_UNOPENED_THINK = re.compile(r"^.*?</think>\s*", re.DOTALL)

# LightRAG 가 llm_model_func 에 넘기지만 Ollama chat() 은 받지 않는 인자들.
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

LlmFunc = Callable[..., Awaitable[str]]


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


def make_llm_func(
    settings: Settings,
    counter: LlmCallCounter,
    client_factory: Callable[[], Any] | None = None,
) -> LlmFunc:
    factory = client_factory or _default_client_factory(settings)

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
            "model": settings.llm_model,
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
