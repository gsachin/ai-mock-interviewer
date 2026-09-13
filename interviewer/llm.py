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
                 transport: httpx.AsyncBaseTransport | None = None,
                 limits: httpx.Limits | None = None):
        self._cfg = config
        self._transport = transport
        # Bounded pool. Without explicit limits httpx defaults to 100 max
        # connections, which is more than a single interview needs and hides
        # a runaway from the operator.
        self._limits = limits or httpx.Limits(
            max_connections=32, max_keepalive_connections=16)
        self._client: httpx.AsyncClient | None = None
        # Kept for the pre-US-008 call pattern (read `llm.metrics` after a
        # call). New callers pass their own object — see respond_stream.
        self.metrics = LLMMetrics()

    async def aclose(self) -> None:
        """Release the pool. Called from the API lifespan and the worker's
        shutdown hook: a long-lived AsyncClient holds sockets open, and a pod
        that is terminating should not wait on them."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        """One client per engine instance, built lazily.

        Previously every call constructed its own AsyncClient, so each LLM
        turn (and every TTS sentence, STT call and MCP request elsewhere in
        the codebase) paid a fresh TCP handshake and TLS negotiation. For a
        local vLLM that is wasted work on the hot path; for a hosted endpoint
        it is worse.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._cfg.timeout,
                transport=self._transport,
                limits=self._limits,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._cfg.token}"} if self._cfg.token else {}

    def _require_model(self) -> str:
        if not self._cfg.model:
            raise ValueError(
                "INTERVIEW_LLM_MODEL is required for LLM turns "
                f"(endpoint {self._cfg.base_url})"
            )
        return self._cfg.model

    def _metrics_target(self, metrics: LLMMetrics | None) -> LLMMetrics:
        """Resolve where this call records its timings.

        A fresh object per call is what makes a SHARED engine safe. Until now
        every engine was constructed per interview and calls were sequential,
        so mutating `self.metrics` never collided — but US-011 shares engines
        per process, and then two concurrent turns would overwrite each
        other's first-token latency. That number feeds the latency SLO, so
        misattributing it corrupts the metric the whole budget is judged by.

        Passing the object in also keeps the attribution honest per hop: the
        brain holds this exact object, so it cannot read a neighbour's.
        """
        if metrics is not None:
            metrics.first_token_ms = None
            metrics.total_ms = 0.0
            return metrics
        # No explicit target: preserve the original contract exactly (a NEW
        # object, so a caller that read `llm.metrics` after the call still
        # sees this call's numbers and not the previous one's).
        self.metrics = LLMMetrics()
        return self.metrics

    async def respond_stream(self, messages: list[dict], *,
                             temperature: float = 0.2,
                             max_tokens: int = 256,
                             metrics: LLMMetrics | None = None) -> AsyncIterator[str]:
        """Yields content deltas; TTS should start on the first sentence, not
        on completion. Metrics land on ``metrics`` (or ``self.metrics``) after
        exhaustion."""
        body = {
            "model": self._require_model(),
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        m = self._metrics_target(metrics)
        client = self._get_client()
        async with client.stream(
            "POST", f"{self._cfg.base_url}/chat/completions",
            json=body, headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                delta = parse_sse_delta(line)
                if delta is not None:
                    if m.first_token_ms is None:
                        m.first_token_ms = (time.perf_counter() - t0) * 1000
                    yield delta
        m.total_ms = (time.perf_counter() - t0) * 1000

    async def respond(self, messages: list[dict], *,
                      temperature: float = 0.2,
                      max_tokens: int = 256,
                      metrics: LLMMetrics | None = None) -> str:
        """Non-streaming completion (evaluation, scoring, summaries)."""
        body = {
            "model": self._require_model(),
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        m = self._metrics_target(metrics)
        resp = await self._get_client().post(
            f"{self._cfg.base_url}/chat/completions",
            json=body, headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        m.total_ms = (time.perf_counter() - t0) * 1000
        choices = data.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content") or ""
