"""Live regression: the Streamlit page actually runs an interview.

Regression target: web/streamlit_app.py gated the worker-thread spawn on
``running``, which is False exactly on the spawn tick — every click ended
instantly with "Interview ended without a summary". This test drives the REAL
page through Streamlit's AppTest runtime (button click, reruns, chat input
patched with scripted answers) against the live RAG + LLM and asserts the
interview wraps with scores.

Marked ``live``: skips when the RAG MCP or Ollama LLM is unreachable.
"""
import os
import queue
import threading
import time

import pytest

from tests.conftest import port_open

RAG_URL = os.environ.get("RAG_MCP_URL", "http://127.0.0.1:8031/mcp")
_HOST, _PORT = (RAG_URL.split("//")[1].split(":")[0],
                int(RAG_URL.rsplit(":", 1)[1].split("/")[0]))

_ANSWERS = [
    "Blue-green shifts all traffic to a parallel environment so rollback is "
    "instant but it doubles infrastructure; canary shifts a small percentage "
    "gradually, exposing fewer users to a bad version.",
    "Layers make rebuilds fast when dependency layers stay cached; ordering "
    "matters so code changes only invalidate the final layers.",
    "Rate, errors and duration measure request-driven services; utilization, "
    "saturation and errors cover resources.",
    "I keep dependency caches keyed correctly so unrelated changes reuse "
    "them, and cache build artifacts separately from source.",
]

_APP = os.path.join(os.path.dirname(__file__), "..", "web", "streamlit_app.py")


@pytest.mark.live
def test_streamlit_page_runs_text_interview_to_wrap():
    if not port_open(_HOST, _PORT):
        pytest.skip(f"no RAG MCP at {_HOST}:{_PORT} — start the stack first")
    if not port_open("127.0.0.1", 11434):
        pytest.skip("Ollama not reachable on :11434")
    streamlit = pytest.importorskip("streamlit")
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    os.environ.setdefault("RAG_MCP_URL", RAG_URL)
    os.environ.setdefault("INTERVIEW_LLM_BASE_URL",
                          "http://127.0.0.1:11434/v1")
    os.environ.setdefault("INTERVIEW_LLM_MODEL", "qwen2.5:14b")
    os.environ.setdefault("INTERVIEW_DOMAIN", "system-design")

    # AppTest has no interactive chat_input — script the candidate's typed
    # answers (repeat the last one so an unexpected follow-up never hangs).
    lock = threading.Lock()
    supply = queue.Queue()
    for a in _ANSWERS:
        supply.put(a)
    state = {"last": _ANSWERS[-1], "uses": 0}

    def fake_chat_input(*args, **kwargs):
        with lock:
            try:
                state["last"] = supply.get_nowait()
            except queue.Empty:
                pass
            state["uses"] += 1
            return state["last"]

    streamlit.chat_input = fake_chat_input  # same module the app imports

    at = AppTest.from_file(os.path.abspath(_APP), default_timeout=120)
    at.run()
    assert not at.exception

    start = next(b for b in at.button if b.label == "Start interview")
    start.click()

    # With the review gate the script settles WITHOUT rerun at each gate
    # (st.stop, no rerun scheduled), so at.run() returns mid-interview —
    # buttons and the feedback card ARE observable between gates now.
    gates_seen = 0
    retake_seen = False
    deadline = time.time() + 420
    while time.time() < deadline:
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        try:
            ctx = at.session_state["ctx"]
        except KeyError:
            ctx = None
        summary = (ctx or {}).get("summary")
        if summary:
            assert summary["state"] == "wrap"
            assert summary["stats"]["questions_asked"] >= 1
            assert summary["scores"], "interview wrapped with no scores"
            thread = (ctx or {}).get("thread")
            assert thread is None or not thread.is_alive()
            assert state["uses"] >= 1, "chat input never drove an answer"
            # every question passed through the review gate, which showed the
            # verdict/model-answer card before Next question
            assert gates_seen == len(summary["scores"]), \
                (f"review gates {gates_seen} != scored questions "
                 f"{len(summary['scores'])}")
            assert retake_seen, "Retake button never appeared at a gate"
            # final per-question review also carries the model answers
            final_md = " ".join(m.value or "" for m in at.markdown)
            assert "Model answer" in final_md
            assert summary["scores"][0]["model_answer"], \
                "score ledger carries the judge's model answer"
            return  # PASSED
        if (ctx or {}).get("error"):
            pytest.fail(f"page error: {ctx['error']}")
        md = " ".join(m.value or "" for m in at.markdown)
        next_btn = [b for b in at.button if b.label == "Next question"]
        if next_btn:
            gates_seen += 1
            assert "Model answer" in md, \
                "review gate rendered without the feedback/model answer"
            retake_seen = retake_seen or any(
                b.label == "Retake answer" for b in at.button)
            next_btn[0].click()          # advance past this question
            continue
        time.sleep(0.5)
    pytest.fail("interview did not complete within 420 s")
