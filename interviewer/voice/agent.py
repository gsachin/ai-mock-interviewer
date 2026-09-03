"""LiveKit agent worker (Phase 3 wiring).

The guarded import keeps the package usable without audio infrastructure:
``livekit-agents`` is an optional extra, and importing this module without
it raises a clear error instead of a silent failure.

Wiring (a plain entrypoint coroutine — no pipeline Agent subclass):
  room audio -> VAD (silero, 16 kHz mono) -> utterance-end speech buffer ->
  STT engine (faster-whisper / deepgram) -> LiveKitCandidate ->
  LLMInterviewer (RAG over MCP, fast voice LLM, sentence-level TTS) ->
  LiveKitAudioSink -> room playback, with barge-in: candidate speech during
  an interviewer turn interrupts playback and generation.
"""
import asyncio
import json
import logging
import time

try:
    from livekit import agents, rtc  # type: ignore  # optional [voice] extra
    from livekit.plugins import silero  # type: ignore
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "livekit-agents is required for the voice worker — "
        "pip install -e '.[voice]'"
    ) from exc

from interviewer.config import InterviewerConfig
from interviewer.rag_client import RagClient
from interviewer.state_machine import Session
from interviewer.voice.interviewer import build_voice_interviewer
from interviewer.voice.livekit import LiveKitAudioSink, LiveKitCandidate
from interviewer.voice.stt import resolve_stt

log = logging.getLogger(__name__)

DOMAINS = ("system-design", "ios", "dsa", "devops")


def domain_from_room(room_name: str, default: str = "system-design") -> str:
    """Rooms are named ``interview-<domain>-<sid>`` by /voice/token."""
    parts = (room_name or "").split("-")
    if len(parts) >= 3 and parts[1] in DOMAINS:
        return parts[1]
    return default


async def run_agent(ctx: agents.JobContext) -> None:
    """One room, one interview: build the voice brain, stream its sentences
    into the room, transcribe candidate utterances (VAD -> STT -> candidate
    queue), and barge in when the candidate speaks over the interviewer."""
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

    async def publish_event(event: dict) -> None:
        """Turn transcripts + the final summary reach the browser as LiveKit
        data packets (topic ``interview``)."""
        try:
            await ctx.room.local_participant.publish_data(
                json.dumps(event).encode(), reliable=True, topic="interview")
        except Exception:
            log.exception("data publish failed")

    interviewer = build_voice_interviewer(config, rag, session, sink=sink,
                                          on_event=publish_event)
    candidate = LiveKitCandidate()
    # One STT engine per room: faster-whisper keeps its model warm across
    # utterances (re-resolving per utterance would reload it every time).
    stt = resolve_stt(config.stt_provider, config)

    vad = silero.VAD.load(sample_rate=16000, activation_threshold=0.5)
    vad_stream = vad.stream()

    async def _consume_vad() -> None:
        """VAD events arrive by iterating the stream (push_frame is
        fire-and-forget). START_OF_SPEECH = barge-in; END_OF_SPEECH carries
        the buffered speech frame -> STT -> the brain's candidate queue."""
        try:
            async for vad_ev in vad_stream:
                if vad_ev.type == agents.vad.VADEventType.START_OF_SPEECH:
                    # barge-in: stop playback/generation (no-op when the
                    # interviewer is not speaking)
                    await interviewer.interrupt()
                elif vad_ev.type == agents.vad.VADEventType.END_OF_SPEECH:
                    if not vad_ev.frames:
                        continue
                    # the frames are 16 kHz mono s16le (the VAD input rate)
                    pcm = b"".join(f.data for f in vad_ev.frames)
                    t0 = time.perf_counter()
                    text = ""
                    try:
                        text = await stt.transcribe(pcm)
                    except Exception:
                        log.exception("STT failed for utterance")
                    stt_ms = (time.perf_counter() - t0) * 1000
                    if text:
                        log.info("candidate said (%d ms): %r",
                                 stt_ms, text[:80])
                        candidate.push(text, stt_ms)
        except Exception:
            log.exception("vad consumer failed")

    async def listen() -> None:
        """Push candidate audio frames into the VAD stream (16 kHz mono —
        the AudioStream resamples)."""
        try:
            stream = await _candidate_audio_stream(participant)
        except Exception:
            log.exception("no candidate audio stream")
            return
        log.info("candidate audio stream attached")
        consumer = asyncio.create_task(_consume_vad())
        try:
            async for ev in stream:
                vad_stream.push_frame(ev.frame)
        except Exception:
            log.exception("audio listen loop failed")
        finally:
            consumer.cancel()

    disconnected = asyncio.Event()

    def _on_disconnect(*args: object) -> None:
        log.info("candidate disconnected (%s) — ending the interview",
                 args[0] if args else "?")
        disconnected.set()

    async def _on_shutdown() -> None:
        # the framework awaits the callback's return — must be a coroutine
        disconnected.set()

    ctx.room.on("participant_disconnected", _on_disconnect)
    ctx.add_shutdown_callback(_on_shutdown)
    log.info("agent in room %r (domain=%s) — interviewer starting",
             ctx.room.name, domain)

    async def run_brain() -> None:
        try:
            summary = await interviewer.run(f"bank-{domain}",
                                            candidate=candidate)
            log.info("interview done: %s state=%s wall=%s voice=%s",
                     summary["session_id"], summary["state"],
                     summary["stats"]["wall_ms"],
                     summary["stats"]["voice_budget_bar"])
        except Exception:
            log.exception("interviewer run failed")

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
    16 kHz mono (the silero VAD's input format)."""
    while True:
        for publication in list(participant.track_publications.values()):
            if (publication.kind == rtc.TrackKind.KIND_AUDIO
                    and publication.track is not None
                    and publication.subscribed):
                return rtc.AudioStream(track=publication.track,
                                       sample_rate=16000, num_channels=1)
        await asyncio.sleep(0.1)
