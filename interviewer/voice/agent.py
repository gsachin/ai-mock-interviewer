"""LiveKit agent worker (Phase 3 wiring).

The guarded import keeps the package usable without audio infrastructure:
``livekit-agents`` is an optional extra, and importing this module without
it raises a clear error instead of a silent failure.

Wiring (a plain entrypoint coroutine — no pipeline Agent subclass):
  room audio -> **manual answer capture** -> STT engine (faster-whisper /
  deepgram) -> LiveKitCandidate -> LLMInterviewer (RAG over MCP, fast voice
  LLM, sentence-level TTS) -> LiveKitAudioSink -> room playback.

Manual capture (the browser's Start answer / Finish answer toggle): the
page's mic track is published continuously, but the worker only buffers
frames while an ``answer_start`` control message has armed it. ``Finish``
transcribes the buffered interval and feeds it to the brain — no VAD
auto-endpointing, so a quiet or gated mic can never swallow an answer the
candidate deliberately recorded. ``Retake`` / ``Next`` control messages
feed the brain's review gate (the decider) after each scored question.
"""
import asyncio
import json
import logging
import time

try:
    from livekit import agents, rtc  # type: ignore  # optional [voice] extra
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "livekit-agents is required for the voice worker — "
        "pip install -e '.[voice]'"
    ) from exc

from interviewer.brain import EmptyBankError, PHASE_LABELS
from interviewer.config import InterviewerConfig
from interviewer.rag_client import RagClient
from interviewer.skills import discover_local_banks
from interviewer.state_machine import Session
from interviewer.voice.interviewer import build_voice_interviewer
from interviewer.voice.livekit import (
    LiveKitAudioSink,
    LiveKitCandidate,
    LiveKitReviewer,
)
from interviewer.voice.stt import resolve_stt
from interviewer.voice.trim import trim_speech_buffer

log = logging.getLogger(__name__)

DOMAINS = ("system-design", "ios", "dsa", "devops")

# Manual-capture bounds: 16 kHz mono s16le = 32 KB/s.
MAX_ANSWER_SECONDS = 120           # safety cap on one recorded answer
_MIN_ANSWER_BYTES = int(0.3 * 16000 * 2)  # shorter buffers are not an answer


def domain_from_room(room_name: str, default: str = "system-design") -> str:
    """Rooms are named ``interview-<domain>-<sid>`` by /voice/token. A room
    parses to a known domain — the static legacy set or any skill currently
    in the question_banks folder. The folder is scanned per call (not at
    import): a skill uploaded to the Skill Update page after this worker
    booted must be recognized without a worker restart."""
    parts = (room_name or "").split("-")
    if len(parts) >= 3 and (parts[1] in DOMAINS
                            or parts[1] in {b.name for b in discover_local_banks()}):
        return parts[1]
    return default


def is_page_event(event: dict) -> bool:
    """True for events the browser renders. The brain's candidate-role turns
    are superseded by ``candidate_heard`` (the STT-first echo) — dropping
    them here prevents duplicate transcript lines on the page."""
    return not (event.get("type") == "turn"
                and event.get("role") == "candidate")


async def run_agent(ctx: agents.JobContext) -> None:
    """One room, one interview: build the voice brain, stream its sentences
    into the room, capture answers the candidate records with the page's
    Start answer / Finish answer toggle, and let the page's Retake / Next
    choices drive the review gate between scored questions."""
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    log.info("candidate joined: %s", participant.identity)

    # Playback: interviewer audio out (48 kHz mono — the sink contract).
    source = rtc.AudioSource(sample_rate=48000, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("interviewer", source)
    await ctx.room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_MICROPHONE))
    sink = LiveKitAudioSink(source, rtc=rtc)

    config = InterviewerConfig.from_env()
    domain = domain_from_room(ctx.room.name)
    rag = RagClient(config.rag_mcp_url, token=config.rag_mcp_token)
    session = Session(session_id=ctx.room.name, tenant_id="default",
                      domain=domain)
    publish_lock = asyncio.Lock()

    async def publish_event(event: dict) -> None:
        """Typed events reach the browser as LiveKit data packets (topic
        ``interview``): ``state`` phase/label, interviewer ``turn`` texts,
        ``candidate_heard`` (STT-first echo), per-question ``score``,
        ``summary``, ``ended``. Candidate-role turns are filtered via
        ``is_page_event``: the candidate's words already reached the page as
        ``candidate_heard`` the moment STT returned — emitting the brain's
        copy too would duplicate the transcript."""
        if not is_page_event(event):
            return
        # Session audit trail: every event the page receives, timestamped by
        # the log formatter — conversation + workflow analysis replays this.
        log.info("event -> page: %s",
                 json.dumps(event, ensure_ascii=False)[:300])
        try:
            # the brain task and the audio/control handlers publish
            # concurrently — serialize so packets never interleave
            async with publish_lock:
                await ctx.room.local_participant.publish_data(
                    json.dumps(event).encode(), reliable=True,
                    topic="interview")
        except Exception:
            log.exception("data publish failed")

    reviewer = LiveKitReviewer()
    interviewer = build_voice_interviewer(config, rag, session, sink=sink,
                                          on_event=publish_event,
                                          decider=reviewer)
    candidate = LiveKitCandidate()
    # One STT engine per room: faster-whisper keeps its model warm across
    # answers (re-resolving per answer would reload it every time).
    stt = resolve_stt(config.stt_provider, config)

    # ── manual answer capture ───────────────────────────────────────────────
    # Mutable capture state shared by the closures below (a dict so nested
    # handlers never fight Python's nonlocal binding rules).
    cap: dict = {"armed": False, "buffer": bytearray(), "busy": False}
    _max_buffer = MAX_ANSWER_SECONDS * 16000 * 2

    async def _notice(text: str) -> None:
        """A soft, non-transcript message (the page shows it as a status)."""
        await publish_event({"type": "notice", "text": text})

    async def _transcribe_answer() -> None:
        """Transcribe the recorded interval (Finish answer) and feed the
        brain. Emits ``state: transcribing`` while STT runs and
        ``candidate_heard`` the instant it returns (RCA 3.0.1).

        RCA 2026-09-03 (multi-minute "Transcribing…"): the armed buffer
        holds everything between Start answer and Finish — including
        thinking-time silence, which whisper decodes at roughly 0.15-0.25x
        real time on CPU int8 (35 s for 150 s of quiet). The buffer is
        energy-trimmed to the speech region FIRST so a short spoken answer
        stays a ~1-3 s decode."""
        pcm = bytes(cap["buffer"])
        raw_seconds = len(pcm) / 32000.0
        cap["buffer"] = bytearray()
        cap["armed"] = False
        if len(pcm) < _MIN_ANSWER_BYTES:
            await _notice("No speech detected — click Start answer and "
                          "try again.")
            return
        pcm = trim_speech_buffer(pcm)      # silence trim (thinking time etc.)
        if len(pcm) < _MIN_ANSWER_BYTES:
            log.info("answer had no speech: %.1f s recorded, trimmed to "
                     "%.1f s", raw_seconds, len(pcm) / 32000.0)
            await _notice("No speech detected — click Start answer and "
                          "try again.")
            return
        log.info("answer: %.1f s recorded -> %.1f s of speech to transcribe",
                 raw_seconds, len(pcm) / 32000.0)
        await publish_event({
            "type": "state", "phase": "transcribing",
            "label": PHASE_LABELS["transcribing"]})
        t0 = time.perf_counter()
        text = ""
        try:
            text = await stt.transcribe(pcm)
        except Exception:
            log.exception("STT failed for recorded answer")
        stt_ms = (time.perf_counter() - t0) * 1000
        if not text.strip():
            await _notice("No speech detected — click Start answer and "
                          "try again.")
            return
        log.info("candidate said (%d ms): %r", stt_ms, text[:80])
        await publish_event({"type": "candidate_heard", "text": text})
        candidate.push(text, stt_ms)

    async def _on_control(msg: dict) -> None:
        """The browser's Start answer / Finish answer / Retake / Next."""
        typ = msg.get("type")
        if typ == "answer_start":
            cap["armed"] = True
            cap["buffer"] = bytearray()
            log.info("answer armed — buffering the candidate's mic")
        elif typ == "answer_cancel":
            cap["armed"] = False
            cap["buffer"] = bytearray()
            log.info("answer cancelled — buffer cleared")
        elif typ == "answer_finish":
            if cap["busy"]:
                return                        # a Finish is already in flight
            cap["busy"] = True
            try:
                await _transcribe_answer()
            except Exception:
                log.exception("answer transcription failed")
                await _notice("Sorry — your answer could not be processed. "
                              "Click Start answer and try again.")
            finally:
                cap["busy"] = False
        elif typ in ("next", "retake"):
            reviewer.push(typ)
            log.info("review decision: %s", typ)
        else:
            log.debug("unknown control message: %r", msg)

    def _on_data(packet: rtc.DataPacket) -> None:
        """Candidate data packets. We only ever receive the page's control
        messages, so the topic is logged for diagnosis but NOT filtered —
        the received topic shape is SDK/server dependent and a strict check
        silently ate every message (the same fragile-filter bug the page had,
        RCA R2)."""
        try:
            msg = json.loads(packet.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.debug("undecodable data packet (topic=%r)", packet.topic)
            return
        if not isinstance(msg, dict) or "type" not in msg:
            log.debug("non-control data packet (topic=%r): %r",
                      packet.topic, msg)
            return
        log.info("control received: %s (topic=%r)", msg["type"],
                 packet.topic)
        asyncio.create_task(_on_control(msg))

    async def listen() -> None:
        """Stream the candidate's mic (16 kHz mono) into the armed buffer —
        nothing is transcribed until the page sends Finish answer."""
        try:
            stream = await _candidate_audio_stream(participant)
        except Exception:
            log.exception("no candidate audio stream")
            return
        log.info("candidate audio stream attached")
        try:
            async for ev in stream:
                if cap["armed"]:
                    cap["buffer"].extend(ev.frame.data)
                    if len(cap["buffer"]) > _max_buffer:   # safety cap
                        log.warning("answer buffer capped at %d s — "
                                    "truncating", MAX_ANSWER_SECONDS)
                        del cap["buffer"][:len(cap["buffer"]) - _max_buffer]
        except Exception:
            log.exception("audio listen loop failed")

    disconnected = asyncio.Event()

    def _on_disconnect(*args: object) -> None:
        log.info("candidate disconnected (%s) — ending the interview",
                 args[0] if args else "?")
        disconnected.set()

    async def _on_shutdown() -> None:
        # the framework awaits the callback's return — must be a coroutine
        disconnected.set()

    ctx.room.on("participant_disconnected", _on_disconnect)
    ctx.room.on("data_received", _on_data)
    ctx.add_shutdown_callback(_on_shutdown)
    log.info("agent in room %r (domain=%s) — interviewer starting",
             ctx.room.name, domain)

    async def run_brain() -> None:
        """Run the FSM to completion, then publish ``ended`` so the page can
        switch to its end-state instead of sitting "Connected" forever (RCA
        R6 / T7)."""
        reason = "interview complete — thank you for practicing"
        try:
            summary = await interviewer.run(f"bank-{domain}",
                                            candidate=candidate)
            log.info("interview done: %s state=%s wall=%s voice=%s",
                     summary["session_id"], summary["state"],
                     summary["stats"]["wall_ms"],
                     summary["stats"]["voice_budget_bar"])
        except EmptyBankError as exc:
            # no questions registered for the room's bank — tell the candidate
            # why instead of a silent zero-question "interview"
            log.warning("empty bank for room %s: %s", ctx.room.name, exc)
            await _notice(str(exc))
            reason = str(exc)
        except Exception:
            log.exception("interviewer run failed")
            reason = "interviewer error — please end the call and try again"
        finally:
            await publish_event({"type": "ended", "reason": reason})

    brain_task = asyncio.create_task(run_brain())
    listen_task = asyncio.create_task(listen())
    done, pending = await asyncio.wait(
        {brain_task, listen_task,
         asyncio.create_task(disconnected.wait())},
        return_when=asyncio.FIRST_COMPLETED)
    log.info("job loop ended (%s done) — cleaning up",
             "brain" if brain_task in done else
             "listen" if listen_task in done else "candidate-left")
    for task in pending:
        task.cancel()
    for task in done:
        if task in (brain_task, listen_task):
            try:
                task.result()  # surface brain/listen errors in the log
            except (asyncio.CancelledError, Exception) as exc:
                log.info("job task ended: %s", exc)


async def _candidate_audio_stream(participant: rtc.RemoteParticipant) -> rtc.AudioStream:
    """Wait for the candidate's subscribed audio track, then stream it as
    16 kHz mono s16le (the manual-capture buffer format)."""
    while True:
        for publication in list(participant.track_publications.values()):
            if (publication.kind == rtc.TrackKind.KIND_AUDIO
                    and publication.track is not None
                    and publication.subscribed):
                return rtc.AudioStream(track=publication.track,
                                       sample_rate=16000, num_channels=1)
        await asyncio.sleep(0.1)
