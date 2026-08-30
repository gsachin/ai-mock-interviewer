"""OpenAICompatibleLLM: SSE parsing, streaming against a mock transport,
non-streaming respond, auth headers, and the model guard."""
import asyncio
import json

import httpx
import pytest

from interviewer.llm import (
    LLMConfig,
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
