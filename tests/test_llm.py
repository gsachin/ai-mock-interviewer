"""OpenAICompatibleLLM: SSE parsing, streaming against a mock transport,
non-streaming respond, auth headers, and the model guard."""
import asyncio
import json

import httpx
import pytest

from interviewer.llm import (
    LLMConfig,
    LLMMetrics,
    OpenAICompatibleLLM,
    parse_sse_delta,
)


def run(coro):
    return asyncio.run(coro)


# ── SSE parser ──────────────────────────────────────────────────────────────

def test_parse_sse_delta_variants():
    assert parse_sse_delta('data: {"choices":[{"delta":{"content":"Hi"}}]}') == "Hi"
    assert parse_sse_delta("data: [DONE]") is None
    assert parse_sse_delta(": keepalive 10/11") is None
    assert parse_sse_delta("data: ") is None
    assert parse_sse_delta("data: not-json") is None
    assert parse_sse_delta('data: {"choices":[]}') is None
    assert parse_sse_delta('data: {"choices":[{"delta":{}}]}') is None
    assert parse_sse_delta("") is None


# ── streaming ───────────────────────────────────────────────────────────────

SSE_BODY = (
    ": keepalive 1/1\n\n"
    'data: {"choices":[{"delta":{"role":"assistant","content":"Hello"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":","}}]}\n\n'
    'data: {"choices":[{"delta":{"content":" world!"}}]}\n\n'
    'data: [DONE]\n\n'
).encode()


def test_respond_stream_collects_deltas_and_metrics():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=SSE_BODY, request=request)

    llm = OpenAICompatibleLLM(
        LLMConfig(base_url="http://llm.test/v1", model="qwen"),
        transport=httpx.MockTransport(handler),
    )
    text = "".join(run(_collect(llm)))
    assert text == "Hello, world!"
    assert llm.metrics.first_token_ms is not None
    assert llm.metrics.total_ms > 0
    assert captured["body"]["model"] == "qwen"
    assert captured["body"]["stream"] is True
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


async def _collect(llm):
    out = []
    async for delta in llm.respond_stream([{"role": "user", "content": "hi"}]):
        out.append(delta)
    return out


def test_respond_stream_sends_bearer_token():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"data: [DONE]\n\n", request=request)

    llm = OpenAICompatibleLLM(
        LLMConfig(base_url="http://llm.test/v1", model="qwen", token="secret"),
        transport=httpx.MockTransport(handler),
    )
    run(_collect(llm))
    assert captured["auth"] == "Bearer secret"


# ── non-streaming ───────────────────────────────────────────────────────────

def test_respond_returns_message_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "full answer"}}],
        }, request=request)

    llm = OpenAICompatibleLLM(
        LLMConfig(base_url="http://llm.test/v1", model="qwen"),
        transport=httpx.MockTransport(handler),
    )
    text = run(llm.respond([{"role": "user", "content": "q"}]))
    assert text == "full answer"
    assert llm.metrics.total_ms > 0


def test_respond_requires_model():
    llm = OpenAICompatibleLLM(LLMConfig(base_url="http://llm.test/v1"))
    with pytest.raises(ValueError, match="INTERVIEW_LLM_MODEL"):
        run(llm.respond([{"role": "user", "content": "q"}]))
    with pytest.raises(ValueError, match="INTERVIEW_LLM_MODEL"):
        run(_collect(llm))


# ── US-008: per-call metrics isolation ──────────────────────────────────────
# The invariant that makes a SHARED engine safe (US-011). Before this, metrics
# lived on the engine instance; engines were built per interview and calls were
# sequential, so nothing collided. Shared per process, two concurrent turns
# would overwrite each other's first-token latency -- the number the latency
# SLO is computed from.

def test_concurrent_calls_do_not_share_metrics():
    """Two overlapping calls on ONE engine must report their own timings."""
    import asyncio
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
        )

    engine = OpenAICompatibleLLM(
        LLMConfig(model="m", base_url="http://test/v1"),
        transport=httpx.MockTransport(handler),
    )

    async def one_call(tag):
        m = LLMMetrics()
        async for _ in engine.respond_stream([{"role": "user", "content": tag}],
                                             metrics=m):
            await asyncio.sleep(0.01)  # force overlap
        return tag, m

    async def main():
        return await asyncio.gather(one_call("a"), one_call("b"))

    results = asyncio.run(main())

    assert len({id(m) for _tag, m in results}) == 2, "calls shared one object"
    for tag, m in results:
        assert m.first_token_ms is not None, f"{tag} lost its first-token timing"
        assert m.total_ms > 0


def test_omitting_metrics_keeps_the_original_contract():
    """Callers that never pass an object must still find timings on
    engine.metrics afterwards — this is the pre-US-008 behaviour and the
    existing tests depend on it."""
    import asyncio
    import httpx

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n')

    engine = OpenAICompatibleLLM(LLMConfig(model="m", base_url="http://test/v1"),
                                 transport=httpx.MockTransport(handler))

    async def main():
        async for _ in engine.respond_stream([{"role": "user", "content": "q"}]):
            pass

    asyncio.run(main())
    assert engine.metrics.first_token_ms is not None
