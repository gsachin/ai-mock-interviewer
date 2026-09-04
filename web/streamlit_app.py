"""Streamlit interview UI (text mode) — drives interviewer.brain.LLMInterviewer.

Run via the launcher:
    .\start_services.ps1 -WithStreamlit        ->  http://localhost:8501
or directly (with the RAG + LLM env exported):
    streamlit run web/streamlit_app.py --server.port 8501 --server.headless true

The UI reuses the exact same engine as `python -m interviewer.demo`: the
interview loop runs in a background thread and blocks on a queue each time a
candidate answer is needed; the chat renders interviewer turns live and
submits the user's typed answers. RAG + LLM wiring comes from the environment
(RAG_MCP_URL, INTERVIEW_LLM_BASE_URL, INTERVIEW_LLM_MODEL) — the same vars
exported by start_services.ps1.
"""
import asyncio
import pathlib
import queue
import sys
import threading
import time

import streamlit as st

# The script lives in web/; the interviewer package lives one level up.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from interviewer.brain import CandidateAnswer, LLMInterviewer  # noqa: E402
from interviewer.config import InterviewerConfig  # noqa: E402
from interviewer.llm import LLMConfig, OpenAICompatibleLLM  # noqa: E402
from interviewer.rag_client import RagClient  # noqa: E402
from interviewer.session_store import InMemorySessionStore, RedisSessionStore  # noqa: E402
from interviewer.skills import discover_local_banks  # noqa: E402
from interviewer.state_machine import Session  # noqa: E402

# Original four domains first (fallback + stable order); every skill found in
# the question_banks folder joins the picker, so a skill registered on the
# Skill Update page is interviewable here without a restart.
_STATIC_DOMAINS = ["system-design", "ios", "dsa", "devops"]
DOMAINS = _STATIC_DOMAINS + sorted(
    {b.name for b in discover_local_banks()} - set(_STATIC_DOMAINS))


class QueueCandidate:
    """Candidate whose answers come from the Streamlit user, one at a time.

    ``answer()`` signals the UI via ``requests`` (the question id it needs an
    answer for) and blocks until the UI pushes the typed text into
    ``responses`` — exactly the wait-for-candidate shape the FSM expects.
    """

    def __init__(self, requests: "queue.Queue[str]", responses: "queue.Queue[str]"):
        self._requests = requests
        self._responses = responses

    async def answer(self, question_id: str,
                     timeout_s: float | None = None,
                     drop_before_ts: float | None = None) -> CandidateAnswer:
        self._requests.put(question_id)
        text = await asyncio.to_thread(self._responses.get)
        return CandidateAnswer(text=text)


class QueueReviewer:
    """Review gate for the text UI (voice-parity pacing): after every scored
    question the brain asks the decider for Retake / Next. ``decide()`` posts
    a ``{"type": "review", ...}`` marker into ``requests`` and blocks until
    the page pushes a choice into ``responses`` — the same shape as
    QueueCandidate, but consumed by the review branch of the page.

    Honors ``timeout_s`` (the brain's review budget, default 90 s): no choice
    in time ⇒ auto-advance (``TimeoutError`` ⇒ the brain moves on), exactly
    like the voice page's gate.
    """

    def __init__(self, requests: "queue.Queue", responses: "queue.Queue"):
        self._requests = requests
        self._responses = responses

    async def decide(self, question_id: str,
                     timeout_s: float | None = None,
                     drop_before_ts: float | None = None) -> str:
        marker = {"type": "review", "question_id": question_id}
        self._requests.put(marker)
        try:
            if timeout_s:
                text = await asyncio.wait_for(
                    asyncio.to_thread(self._responses.get), timeout_s)
            else:
                text = await asyncio.to_thread(self._responses.get)
        except asyncio.TimeoutError as exc:
            try:
                self._requests.queue.remove(marker)  # never leave a stale gate
            except ValueError:
                pass
            raise TimeoutError("review decision timed out") from exc
        return text if text in ("retake", "next") else "next"


def _run_interview(ctx: dict) -> None:
    """Worker: drive the full FSM to completion inside one asyncio loop."""
    try:
        config = ctx["config"]
        rag = RagClient(config.rag_mcp_url, token=config.rag_mcp_token)
        llm = OpenAICompatibleLLM(LLMConfig(
            base_url=config.llm_base_url, model=config.llm_model, token=config.llm_token))
        interviewer = LLMInterviewer(rag, llm, ctx["session"],
                                     max_questions=ctx["max_questions"],
                                     decider=ctx["reviewer"])
        summary = asyncio.run(interviewer.run(ctx["doc_id"], candidate=ctx["candidate"]))
        store = (RedisSessionStore(config.redis_url) if config.session_store == "redis"
                 else InMemorySessionStore())
        asyncio.run(store.save(summary["session_id"], summary))
        ctx["summary"] = summary
    except Exception as exc:  # surface any failure to the UI, don't die silently
        ctx["error"] = f"{type(exc).__name__}: {exc}"


def _turn_parts(turn) -> tuple:
    if isinstance(turn, dict):
        return turn.get("role"), turn.get("text")
    return getattr(turn, "role", None), getattr(turn, "text", None)


_VERDICT_GLYPH = {"correct": "✅", "partial": "◐", "incorrect": "❌"}


def _feedback_text(entry: dict) -> str:
    """One question's judge feedback as markdown — verdict, per-dimension
    scores, gap, and the model answer. Used both live (latest question under
    the chat) and in the final per-question review (voice-page parity)."""
    glyph = _VERDICT_GLYPH.get((entry.get("verdict") or "").lower(), "•")
    chips = " · ".join(f"{k.title()} {v}" for k, v in entry["scores"].items())
    gap = (entry.get("justifications") or "").strip()
    parts = [f"**{glyph} {entry.get('section_title', 'Question')} — "
             f"{entry.get('verdict', 'scored')}** · {chips}"]
    if gap:
        parts.append(f"*Gap: {gap[:300]}*")
    if entry.get("model_answer"):
        parts.append(f"**Model answer:** {entry['model_answer']}")
    return "\n\n".join(parts)


st.set_page_config(page_title="AI Mock Interviewer", page_icon="🎤", layout="wide")
st.title("🎤 AI Mock Interviewer")
st.caption("Text-mode interviewer — same engine as `interviewer.demo`, retrieving "
           "rubrics and domain context from the enterprise-rag-core MCP service.")

config = InterviewerConfig.from_env()

if "ctx" not in st.session_state:
    st.session_state.ctx = None

ctx = st.session_state.ctx
running = bool(ctx and ctx.get("thread") and ctx["thread"].is_alive())

# ---- Sidebar ----------------------------------------------------------------
with st.sidebar:
    st.header("Interview setup")
    default_idx = DOMAINS.index(config.default_domain) if config.default_domain in DOMAINS else 0
    domain = st.selectbox("Domain", DOMAINS, index=default_idx, disabled=running)
    max_q = st.slider("Number of questions", 1, 5, 2, disabled=running)
    doc_id = st.text_input("Question bank (doc-id)", value=f"bank-{domain}", disabled=running)
    st.divider()
    st.caption(f"RAG MCP: `{config.rag_mcp_url}`")
    st.caption(f"LLM: `{config.llm_model or 'NOT SET'}` @ {config.llm_base_url}")
    start_clicked = st.button("Start interview", type="primary", disabled=running)

if start_clicked:
    if not config.llm_model:
        st.error("INTERVIEW_LLM_MODEL is not set. Run `start_services.ps1` (it exports "
                 "Ollama qwen2.5:14b) or export it before launching Streamlit.")
    else:
        st.session_state.ctx = {
            "config": config,
            "domain": domain,
            "doc_id": doc_id,
            "max_questions": max_q,
            "session_id": time.strftime("ui-%m%d%H%M%S"),
            "requests": queue.Queue(),
            "responses": queue.Queue(),
            "thread": None,
            "session": None,
            "candidate": None,
            "reviewer": None,
            "summary": None,
            "error": None,
        }
        st.rerun()

if not ctx:
    st.info("Configure the interview in the sidebar and press **Start interview**.")
    st.stop()

# ---- Chat area --------------------------------------------------------------
messages = st.container(height=480)

turns = list(ctx["session"].turns) if ctx.get("session") else []
for turn in turns:
    role, text = _turn_parts(turn)
    with messages.chat_message("assistant" if role == "interviewer" else "user"):
        st.markdown(text)

# Voice-parity per-question feedback: the judge's verdict + model answer ride
# on session.scores as each question completes (brain.py score ledger), but
# unlike the voice page the text UI never rendered them until the final
# summary. While the interview runs, show the latest scored question in a
# card below the chat — the same feedback the voice page shows before
# Retake/Next.
if running and ctx.get("session") and ctx["session"].scores:
    st.markdown("---")
    st.markdown(_feedback_text(ctx["session"].scores[-1]))

if ctx.get("error"):
    st.error(ctx["error"])
    st.stop()

# Spawn the worker on the tick where the interview is configured but no
# thread exists yet. (This MUST NOT be gated on ``running``: on that first
# tick the thread is None so ``running`` is false — gating here left the
# worker unstarted and every interview "ended without a summary" instantly.)
if ctx.get("thread") is None:
    ctx["candidate"] = QueueCandidate(ctx["requests"], ctx["responses"])
    ctx["reviewer"] = QueueReviewer(ctx["requests"], ctx["responses"])
    ctx["session"] = Session(session_id=ctx["session_id"], tenant_id="default",
                             domain=ctx["domain"])
    ctx["thread"] = threading.Thread(target=_run_interview, args=(ctx,),
                                     daemon=True)
    ctx["thread"].start()
    st.rerun()

if ctx["thread"] and ctx["thread"].is_alive():
    st.caption(f"State: `{ctx['session'].state.value}`")

    # Peek the newest request the worker is waiting on (entries are removed
    # once answered, so at most one pending request is live at a time).
    pending = list(ctx["requests"].queue)
    last_req = pending[-1] if pending else None
    waiting_review = (isinstance(last_req, dict)
                      and last_req.get("type") == "review")

    if waiting_review:
        # Review gate (voice parity): the feedback card above shows the
        # judge's verdict + correct answer for the scored question — the
        # interview pauses here until the candidate chooses.
        st.caption("Feedback for your answer is above — Retake to answer "
                   "this question again, or continue to the next question.")
        col_retake, col_next = st.columns(2)
        qid = last_req.get("question_id", "")
        if col_next.button("Next question", type="primary",
                           key=f"gate-next-{qid}"):
            ctx["responses"].put("next")
            ctx["requests"].queue.remove(last_req)
            st.rerun()
        if col_retake.button("Retake answer", key=f"gate-retake-{qid}"):
            ctx["responses"].put("retake")
            ctx["requests"].queue.remove(last_req)
            st.rerun()
        st.stop()

    if last_req is not None:
        # The interviewer is waiting for this turn's answer.
        answer = st.chat_input("Your answer (text)…")
        if answer:
            ctx["responses"].put(answer)
            ctx["requests"].queue.remove(last_req)
            st.rerun()
    else:
        st.info("Evaluating your answer / preparing the next step — the judge "
                "runs on qwen2.5:14b, expect a few seconds up to ~1 minute. "
                "Feedback appears under the chat as each question is scored.")
        time.sleep(1)
        st.rerun()
    st.stop()

# ---- Finished ---------------------------------------------------------------
summary = ctx.get("summary")
if summary:
    st.success("Interview complete.")
    stats = summary.get("stats") or {}
    col1, col2, col3 = st.columns(3)
    col1.metric("Average score", f"{stats.get('average_score', '-')} / 5")
    col2.metric("Rubric cache hit rate", f"{stats.get('cache_hit_rate', '-')}")
    col3.metric("Questions asked", stats.get("questions_asked", "-"))
    if summary.get("scores"):
        rows = [{
            "Question": e["question_id"],
            "Section": e["section_title"],
            "Scores": " / ".join(str(v) for v in e["scores"].values()),
            "Follow-up": "yes" if e.get("followup_asked") else "no",
        } for e in summary["scores"]]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.markdown("**Per-question feedback**")
        for entry in summary["scores"]:
            st.markdown(_feedback_text(entry))
    st.caption(f"Session `{summary['session_id']}` · {summary['state']} · "
               f"wall {stats.get('wall_ms', '-')} ms · "
               f"LLM first-token mean {stats.get('llm_first_token_mean_ms', '-')} ms · "
               f"RAG total mean {stats.get('rag_total_mean_ms', '-')} ms")
    if st.button("New interview", type="secondary"):
        st.session_state.ctx = None
        st.rerun()
else:
    st.warning("Interview ended without a summary (see errors above).")
    if st.button("New interview", type="secondary"):
        st.session_state.ctx = None
        st.rerun()
