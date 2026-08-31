"""Phase 3 live gate: real RAG MCP + the fast voice LLM (llama3.2:3b via
Ollama) + simulated engine latencies (STT 300 ms, TTS 150 ms — stand-ins
for Deepgram/Cartesia-class engines).

Gate assertion: the five-stage voice bar (utterance-end -> first
interviewer audio: STT + RAG + LLM first token + TTS first audio +
network) must stay under 1500 ms. The judge's wait is measured separately
(judge_wait_ms) — it is the candidate's real additional wait today and the
known target of the question-generation overlap optimization.
"""
import asyncio
import os

import pytest

from interviewer.brain import LLMInterviewer
from interviewer.llm import LLMConfig, OpenAICompatibleLLM
from interviewer.rag_client import RagClient
from interviewer.state_machine import Session
from interviewer.voice.budget import BUDGET_MS, LatencyBudgetTracker
from interviewer.voice.interviewer import AudioCandidate
from tests.conftest import port_open
from tests.test_live_phase2 import ANSWERS


class FakeSTT:
    """Fixed 300 ms transcription latency; transcript rides inside the bytes."""

    def __init__(self, delay_ms: float = 300.0):
        self._delay_ms = delay_ms

    async def transcribe(self, audio: bytes) -> str:
        await asyncio.sleep(self._delay_ms / 1000)
        text = audio.decode(errors="replace")
        return text.removeprefix("TRANSCRIPT:") if text.startswith("TRANSCRIPT:") else ""


class FakeTTS:
    """Fixed 150 ms first-audio latency per sentence."""

    def __init__(self, delay_ms: float = 150.0):
        self._delay_ms = delay_ms

    async def synthesize(self, text: str) -> bytes:
        await asyncio.sleep(self._delay_ms / 1000)
        return b""


def run(coro):
    return asyncio.run(coro)


def _services():
    rag_url = os.environ.get("RAG_MCP_URL", "http://127.0.0.1:8031/mcp")
    judge_base = os.environ.get("INTERVIEW_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    judge_model = os.environ.get(
        "INTERVIEW_LLM_MODEL", "mlx-community/Qwen2.5-14B-Instruct-4bit")
    voice_base = os.environ.get("INTERVIEW_VOICE_LLM_BASE_URL",
                                "http://localhost:11434/v1")
    voice_model = os.environ.get("INTERVIEW_VOICE_LLM_MODEL", "llama3.2:3b")
    return rag_url, judge_base, judge_model, voice_base, voice_model


@pytest.mark.live
def test_voice_gate_two_sessions_under_1500ms():
    rag_url, judge_base, judge_model, voice_base, voice_model = _services()

    rag_host, rag_port = "127.0.0.1", int(rag_url.split(":")[2].split("/")[0])
    if not port_open(rag_host, rag_port):
        pytest.skip(f"no RAG MCP at {rag_url}")
    judge_port = int(judge_base.split(":")[2].split("/")[0])
    if not port_open("127.0.0.1", judge_port):
        pytest.skip(f"no judge LLM at {judge_base}")
    voice_port = int(voice_base.split(":")[2].split("/")[0])
    if not port_open("127.0.0.1", voice_port):
        pytest.skip(f"no voice LLM at {voice_base}")

    rag = RagClient(rag_url)
    judge_llm = OpenAICompatibleLLM(LLMConfig(base_url=judge_base, model=judge_model))
    voice_llm = OpenAICompatibleLLM(LLMConfig(base_url=voice_base, model=voice_model))

    async def one(session_id):
        budget = LatencyBudgetTracker()
        brain = LLMInterviewer(
            rag, judge_llm,
            Session(session_id=session_id, tenant_id="default",
                    domain="system-design"),
            tts=FakeTTS(), voice_llm=voice_llm, budget=budget,
        )
        audio = {qid: f"TRANSCRIPT:{text}".encode() for qid, text in ANSWERS.items()}
        summary = await brain.run("bank-system-design",
                                  candidate=AudioCandidate(FakeSTT(), audio))
        return summary

    summaries = [run(one(f"voice-gate-{i}")) for i in range(2)]
    for summary in summaries:
        assert summary["state"] == "wrap", summary["state"]
        for entry in summary["scores"]:
            assert set(entry["scores"]) >= {"correctness", "depth"}, entry
        agg = summary["stats"]["voice_budget"]
        five_bar_ms = (agg["stt_final_ms"] + agg["rag_ms"]
                       + agg["llm_first_token_ms"] + agg["tts_first_audio_ms"]
                       + agg["network_ms"])
        assert agg["turns"] >= 4, agg
        assert agg["judge_wait_ms"] > 0, "judge wait must be measured"
        assert five_bar_ms < BUDGET_MS, (
            f"voice bar {five_bar_ms:.0f}ms >= {BUDGET_MS:.0f}ms budget\n"
            f"{summary['stats']['voice_budget_bar']}")
