import asyncio

from specgraph.llm import LlmCallCounter, make_llm_func, strip_think


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
