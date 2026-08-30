"""ScriptedInterview: unit tests against a stub RagClient, plus a live
end-to-end run (marked ``live``) against the standalone RAG MCP service."""
import asyncio

import pytest

from interviewer.interview import ScriptedInterview
from interviewer.rag_client import (
    InterviewBankResult,
    InterviewQuestionChunk,
    InterviewQuestionResult,
    QuestionRef,
)
from interviewer.state_machine import InterviewerState, Session


class StubRag:
    """Records calls; rubric hit_source alternates retrieval -> cache so the
    cache-hit accounting is exercised deterministically."""

    def __init__(self, questions: list[str] | None = None):
        self._titles = questions or ["Rate limiter design", "Consistent hashing"]
        self.bank_calls: list[str] = []
        self.rubric_queries: list[str] = []
        self.followup_calls: list[tuple[str, str, int]] = []
        self._rubric_calls = 0

    async def interview_bank(self, doc_id):
        self.bank_calls.append(doc_id)
        return InterviewBankResult(
            doc_id=doc_id, tenant_id="default",
            questions=[
                QuestionRef(question_id=f"s{i + 1}", section_title=t, chunk_count=1)
                for i, t in enumerate(self._titles)
            ],
            count=len(self._titles),
        )

    async def interview_question(self, doc_id, question_id):
        return InterviewQuestionResult(
            doc_id=doc_id, tenant_id="default", question_id=question_id,
            section_title=f"Question {question_id}",
            chunks=[InterviewQuestionChunk(chunk_id=f"{doc_id}:{question_id}:c1",
                                           content=f"body of {question_id}")],
        )

    async def agent_context(self, resume_text, job_description, rubric_query,
                            channel="voice"):
        self.rubric_queries.append(rubric_query)
        self._rubric_calls += 1
        return {
            "status": "SUCCESS",
            "hit_source": "cache" if self._rubric_calls > 1 else "retrieval",
            "context_envelope": f"[context_envelope tenant=default clearance>=0]",
            "provenance": [
                {"chunk_id": "rub1", "source": "rubric", "score": 0.9},
            ],
            "timings_ms": {"total": 12.3},
        }

    async def interview_followup(self, query, domain="", top_k=3):
        self.followup_calls.append((query, domain, top_k))
        from interviewer.rag_client import Chunk, RetrieveContextResult
        return RetrieveContextResult(
            chunks=[Chunk(chunk_id="f1", parent_id="bank", tenant_id="default",
                          content="followup material", department=domain)],
            count=1,
        )


def _session(domain="system-design") -> Session:
    return Session(session_id="s1", tenant_id="default", domain=domain)


def test_scripted_interview_runs_full_loop():
    rag = StubRag()
    script = {"s1": "token bucket with redis counters", "s2": "virtual nodes"}
    interview = ScriptedInterview(rag, _session(), script=script)

    summary = asyncio.run(interview.run("bank-sd", max_questions=2))

    assert summary["state"] == InterviewerState.WRAP.value
    assert summary["stats"]["questions_asked"] == 2
    # rubric retrieval ran per question; the stub's second call is a cache hit
    assert summary["stats"]["rubric_retrievals"] == 2
    assert summary["stats"]["rubric_cache_hits"] == 1
    assert summary["stats"]["cache_hit_rate"] == 0.5
    assert summary["stats"]["followups"] == 2
    # turns alternate interviewer / candidate
    assert [t["role"] for t in summary["turns"]] == [
        "interviewer", "candidate", "interviewer", "candidate"]
    assert summary["turns"][0]["text"].startswith("Question s1")
    # scores carry the per-question ledger
    assert [s["question_id"] for s in summary["scores"]] == ["s1", "s2"]
    assert summary["scores"][1]["rubric_hit_source"] == "cache"
    # follow-up retrieval is domain-scoped with the session domain
    assert rag.followup_calls == [
        ("token bucket with redis counters", "system-design", 3),
        ("virtual nodes", "system-design", 3),
    ]


def test_scripted_interview_caps_question_count():
    rag = StubRag(["q1", "q2", "q3", "q4"])
    interview = ScriptedInterview(rag, _session(), script={})
    summary = asyncio.run(interview.run("bank", max_questions=1))
    assert summary["stats"]["questions_asked"] == 1
    assert rag.rubric_queries == ["q1"]


@pytest.mark.live
def test_scripted_interview_live_against_core():
    """Phase 1 gate: two consecutive interviews over the real RAG MCP — the
    second run must hit the semantic cache on rubric queries, and follow-up
    chunks must stay inside the interview domain."""
    import os

    from interviewer.rag_client import RagClient
    from tests.conftest import port_open

    url = os.environ.get("RAG_MCP_URL", "http://127.0.0.1:8000/mcp")
    host, port = url.split("//")[1].split(":")[0], int(url.rsplit(":", 1)[1].split("/")[0])
    if not port_open(host, port):
        pytest.skip(f"no service at {host}:{port} — start enterprise-rag-core serve")

    rag = RagClient(url)
    script = {
        "s1": "token bucket with redis counters and 429 responses",
        "s2": "virtual nodes on a hash ring minimize reshuffling",
    }

    async def one_run(session_id):
        return await ScriptedInterview(
            rag, Session(session_id=session_id, tenant_id="default",
                         domain="system-design"),
            script=script,
        ).run("bank-system-design", max_questions=2)

    first = asyncio.run(one_run("live-1"))
    assert first["state"] == InterviewerState.WRAP.value
    assert first["stats"]["questions_asked"] == 2

    # per-domain isolation: every follow-up chunk belongs to the interview domain
    followups = asyncio.run(rag.interview_followup(
        "token bucket sliding window redis", domain="system-design", top_k=3))
    assert followups.count >= 1
    assert all(c.department == "system-design" for c in followups.chunks)

    # cache: the second identical interview reuses the semantic cache
    second = asyncio.run(one_run("live-2"))
    assert second["stats"]["rubric_cache_hits"] >= 1, second["stats"]
    assert second["stats"]["cache_hit_rate"] >= 0.5, second["stats"]
