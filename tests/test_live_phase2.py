"""Phase 2 live smoke: two recorded interviews with the real RAG MCP service
AND a real OpenAI-compatible LLM (the deployed MLX server by default).
Skipped unless both endpoints are reachable — the full 10-session gate is
run via ``scripts/run_gate.py`` and recorded under ``gate_results/``."""
import asyncio
import os

import pytest

from interviewer.brain import LLMInterviewer
from interviewer.llm import LLMConfig, OpenAICompatibleLLM
from interviewer.rag_client import RagClient
from interviewer.state_machine import Session
from tests.conftest import port_open

ANSWERS = {
    "s1": "Token bucket with per-user keys in Redis, atomic INCR with expiry, "
          "and 429 responses with retry-after headers.",
    "s1:followup": "Sliding windows smooth bursts better than fixed windows, "
                   "at the cost of more state.",
    "s2": "Virtual nodes on a hash ring, so only K/N keys move when a node "
          "joins or leaves.",
    "s2:followup": "Distributed caches and sharded databases use it to keep "
                   "reshuffling minimal.",
}


def _services():
    rag_url = os.environ.get("RAG_MCP_URL", "http://127.0.0.1:8031/mcp")
    llm_base = os.environ.get("INTERVIEW_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    llm_model = os.environ.get(
        "INTERVIEW_LLM_MODEL", "mlx-community/Qwen2.5-14B-Instruct-4bit")
    return rag_url, llm_base, llm_model


@pytest.mark.live
def test_two_recorded_interviews_with_real_services():
    rag_url, llm_base, llm_model = _services()

    rag_host, rag_port = "127.0.0.1", int(rag_url.split(":")[2].split("/")[0])
    if not port_open(rag_host, rag_port):
        pytest.skip(f"no RAG MCP at {rag_url}")
    llm_host, llm_port = "127.0.0.1", int(llm_base.split(":")[2].split("/")[0])
    if not port_open(llm_host, llm_port):
        pytest.skip(f"no LLM endpoint at {llm_base} — start the MLX/vLLM server")

    rag = RagClient(rag_url)
    llm = OpenAICompatibleLLM(LLMConfig(base_url=llm_base, model=llm_model))

    async def one(session_id):
        return await LLMInterviewer(rag, llm, Session(
            session_id=session_id, tenant_id="default",
            domain="system-design")).run("bank-system-design", answers=ANSWERS)

    summaries = [asyncio.run(one(f"live2-{i}")) for i in range(2)]
    for summary in summaries:
        assert summary["state"] == "wrap"
        # the real judge produced parseable scores on every question
        for entry in summary["scores"]:
            assert set(entry["scores"]) >= {"correctness", "depth"}, entry
            assert all(1 <= v <= 5 for v in entry["scores"].values())
        # per-hop latency was logged end to end
        assert summary["stats"]["llm_first_token_mean_ms"] is not None
        assert summary["stats"]["rag_total_mean_ms"] is not None
