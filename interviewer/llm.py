"""Streaming OpenAI-compatible LLM client (vLLM / Ollama / OpenAI / MLX).

No new dependencies — httpx SSE over ``aiter_lines``, hand-parsed. Implements
the LLMEngine protocol from ``interviewer.voice.protocols``. Per-call latency
metrics (first-token / total ms) are recorded on ``self.metrics`` — the
interviewer logs them per hop.

Known-good endpoints:
  vLLM   http://127.0.0.1:8000/v1
  MLX    http://127.0.0.1:1234/v1   (mlx_lm.server — chat only, no embeddings)
  Ollama http://localhost:11434/v1
  OpenAI https://api.openai.com/v1
"""
import json
import time
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass(frozen=True)
class LLMConfig:
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str | None = None        # required for LLM turns (INTERVIEW_LLM_MODEL)
    token: str | None = None        # bearer for hosted endpoints
    timeout: float = 300.0          # shared 14B-class servers can take minutes


@dataclass
class LLMMetrics:
    first_token_ms: float | None = None
    total_ms: float = 0.0


def parse_sse_delta(line: str) -> str | None:
    """One SSE line -> content delta, or None (keepalives, ``[DONE]``,
    non-data lines, chunks without content)."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices") or []
    if not choices:
        return None
    return choices[0].get("delta", {}).get("content") or None


class OpenAICompatibleLLM:
    """Streaming chat-completions client with a test-only transport seam."""

    def __init__(self, config: LLMConfig, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._cfg = config
        self._transport = transport
        self.metrics = LLMMetrics()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._cfg.token}"} if self._cfg.token else {}

    def _require_model(self) -> str:
        if not self._cfg.model:
            raise ValueError(
                "INTERVIEW_LLM_MODEL is required for LLM turns "
                f"(endpoint {self._cfg.base_url})"
            )
        return self._cfg.model

    async def respond_stream(self, messages: list[dict], *,
                             temperature: float = 0.2,
                             max_tokens: int = 256) -> AsyncIterator[str]:
        """Yields content deltas; TTS should start on the first sentence, not
        on completion. Metrics land on ``self.metrics`` after exhaustion."""
        body = {
            "model": self._require_model(),
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        self.metrics = LLMMetrics()
        async with httpx.AsyncClient(timeout=self._cfg.timeout,
                                     transport=self._transport) as client:
            async with client.stream(
                "POST", f"{self._cfg.base_url}/chat/completions",
                json=body, headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    delta = parse_sse_delta(line)
                    if delta is not None:
                        if self.metrics.first_token_ms is None:
                            self.metrics.first_token_ms = (time.perf_counter() - t0) * 1000
                        yield delta
        self.metrics.total_ms = (time.perf_counter() - t0) * 1000

    async def respond(self, messages: list[dict], *,
                      temperature: float = 0.2,
                      max_tokens: int = 256) -> str:
        """Non-streaming completion (evaluation, scoring, summaries)."""
        body = {
            "model": self._require_model(),
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        self.metrics = LLMMetrics()
        async with httpx.AsyncClient(timeout=self._cfg.timeout,
                                     transport=self._transport) as client:
            resp = await client.post(
                f"{self._cfg.base_url}/chat/completions",
                json=body, headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        self.metrics.total_ms = (time.perf_counter() - t0) * 1000
        choices = data.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content") or ""
