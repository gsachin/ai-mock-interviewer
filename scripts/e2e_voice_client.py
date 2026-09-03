"""Phase 3 live E2E: a scripted candidate joins a voice interview room.

Flow: POST /voice/token -> join the LiveKit room -> the agent worker is
dispatched -> the interviewer greets/asks via TTS (audio track) -> this
client plays prepared answers (Kokoro-synthesized) after each interviewer
turn -> the worker transcribes (faster-whisper), judges, and finally
publishes the interview summary as a data packet.

Assertions: interviewer audio arrives, every prepared answer is consumed or
dropped without breaking the flow, the summary reaches state ``wrap`` with
scores and a voice budget bar.

Usage (with the full stack up — see the README voice runbook):
    python scripts/e2e_voice_client.py [--domain system-design] [--answers N]
"""
import argparse
import asyncio
import json
import sys
import time

import httpx
import numpy as np
from livekit import rtc

sys.path.insert(0, ".")
from interviewer.config import InterviewerConfig  # noqa: E402
from interviewer.voice.tts import resolve_tts  # noqa: E402

ANSWERS = [
    "I would use a token bucket with Redis counters and Lua scripts for atomic refills.",
    "To make it distributed, shard the buckets by key and replicate the counters.",
    "For consistency, I would accept eventual consistency and trade exact limits for availability.",
    "I would monitor the system with per key rate metrics and alert on throttle spikes.",
    "The wrap up: overall I think the design balances latency and correctness well.",
]


class CandidateClient:
    def __init__(self, token: str, url: str, tts, answer_texts: list[str]):
        self._token = token
        self._url = url
        self._tts = tts
        self._answer_texts = list(answer_texts)
        self._room = rtc.Room()
        self._events: list[dict] = []
        self._interviewer_frames = 0
        self._speaking = False
        self._mic_source: rtc.AudioSource | None = None
        self._used = 0
        self._done = asyncio.Event()
        self.summary: dict | None = None

    async def run(self, timeout: float = 420.0) -> dict:
        self._room.on("track_subscribed", self._on_track)
        self._room.on("data_received", self._on_data)
        await self._room.connect(self._url, self._token,
                                 options=rtc.RoomOptions(auto_subscribe=True))
        print(f"[client] joined room {self._room.name}")
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
        except asyncio.TimeoutError:
            print("[client] TIMEOUT — no summary received")
        finally:
            await asyncio.sleep(1.0)  # grace: let the worker's `ended` land
            await self._room.disconnect()
        if self.summary is None:
            raise RuntimeError("no summary received from the interviewer")
        return self.summary

    def _on_track(self, track: rtc.Track, publication: rtc.RemoteTrackPublication,
                  participant: rtc.RemoteParticipant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"[client] interviewer audio track subscribed "
                  f"({publication.sid})")
            asyncio.create_task(self._count_frames(track))
            asyncio.create_task(self._answer_loop())

    async def _count_frames(self, track: rtc.Track):
        stream = rtc.AudioStream(track=track, sample_rate=48000, num_channels=1)
        async for _ in stream:
            self._interviewer_frames += 1

    def _on_data(self, packet: rtc.DataPacket, *args):
        try:
            event = json.loads(packet.data)
        except (json.JSONDecodeError, TypeError):
            return
        if event.get("type") == "turn":
            print(f"[client] turn: {event['role']}: {event['text'][:90]!r}")
            self._events.append(event)
        elif event.get("type") == "candidate_heard":
            print(f"[client] heard me: {event['text'][:60]!r}")
        elif event.get("type") == "score":
            print(f"[client] score: {event['score']['question_id']} "
                  f"{event['score']['scores']}")
        elif event.get("type") == "summary":
            self.summary = event["summary"]   # compact: {state, scores, stats}
            print("[client] SUMMARY received — interview complete")
            self._done.set()
        elif event.get("type") == "ended":
            self.ended = event
            print(f"[client] ENDED: {event.get('reason', '')}")
            self._done.set()

    def _new_turn_stage(self, seen: int) -> str | None:
        """The stage of the next interviewer turn that asks for an answer
        (None until one arrives)."""
        for ev in self._events[seen:]:
            if ev.get("role") == "interviewer" and ev.get("stage") in (
                    "question", "followup"):
                return ev["stage"]
        return None

    async def _answer_loop(self):
        """Answer every question/follow-up the interviewer asks, in order.
        The brain emits staged turns — a stage of ``question``/``followup``
        means it is listening. Synthesis happens lazily (the join must not
        wait for it)."""
        await asyncio.sleep(1.0)  # let the greeting play out
        turns_seen = 0
        while not self._done.is_set():
            stage = self._new_turn_stage(turns_seen)
            if stage is None:
                await asyncio.sleep(0.2)
                continue
            turns_seen = len(self._events)
            await asyncio.sleep(1.5)  # question playout + silence
            if self._done.is_set():
                return
            # reuse the text that best answers the stage; loop around if the
            # interviewer asks more than we prepared
            text = self._answer_texts[self._used
                                      % len(self._answer_texts)]
            self._used += 1
            audio = await self._tts.synthesize(text)
            await self._speak(audio)
            print(f"[client] answered {stage} ({self._used}"
                  f"/{len(self._answer_texts)} prepared): {text[:50]}…")

    async def _speak(self, pcm: bytes) -> None:
        """Play one answer through the persistent mic track (48 kHz mono
        s16le). The track is published once on first use and reused."""
        if self._mic_source is None:
            self._mic_source = rtc.AudioSource(sample_rate=48000, num_channels=1)
            track = rtc.LocalAudioTrack.create_audio_track("candidate-mic",
                                                           self._mic_source)
            await self._room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(
                    source=rtc.TrackSource.SOURCE_MICROPHONE))
            print("[client] candidate mic published")
        self._speaking = True
        frame_samples = 48000 * 20 // 1000
        frame_bytes = frame_samples * 2
        for i in range(0, len(pcm), frame_bytes):
            data = pcm[i:i + frame_bytes]
            if len(data) < frame_bytes:
                data += b"\x00" * (frame_bytes - len(data))
            await self._mic_source.capture_frame(rtc.AudioFrame(
                data=data, sample_rate=48000, num_channels=1,
                samples_per_channel=frame_samples))
            await asyncio.sleep(0.02)  # real-time playout
        # a beat of silence before the VAD endpointing closes the utterance
        await asyncio.sleep(0.9)
        self._speaking = False


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default="system-design")
    parser.add_argument("--backend", default="http://127.0.0.1:8010")
    parser.add_argument("--answers", type=int, default=5,
                        help="number of prepared answers to synthesize")
    args = parser.parse_args()

    t0 = time.perf_counter()
    print(f"[client] requesting token (domain={args.domain})")
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(f"{args.backend}/voice/token",
                               json={"domain": args.domain})
        resp.raise_for_status()
        body = resp.json()
    print(f"[client] room={body['room']} session={body['session_id']}")

    config = InterviewerConfig(tts_provider="kokoro")
    tts = resolve_tts("kokoro", config)

    client = CandidateClient(body["token"], body["livekit_url"], tts,
                             ANSWERS[:args.answers])
    summary = await client.run()

    stats = summary["stats"]
    interviewer_turns = [e for e in client._events
                         if e.get("role") == "interviewer"]
    bar = (stats["voice_budget_bar"] or "").replace("→", "->") \
        .replace("✓", "OK").replace("✗", "FAIL")
    print("\n=== E2E RESULT ===")
    print(f"state: {summary['state']}  questions: {stats['questions_asked']}")
    print(f"interviewer turns: {len(interviewer_turns)}  "
          f"scores: {len(summary['scores'])}")
    print(f"interviewer audio frames received: {client._interviewer_frames}")
    print(f"ended event: {getattr(client, 'ended', None) is not None}")
    print(f"voice budget: {bar}")
    print(f"cache hit rate: {stats['cache_hit_rate']}  "
          f"wall: {stats['wall_ms']} ms  total: {time.perf_counter() - t0:.0f} s")
    for entry in summary["scores"]:
        print(f"  {entry['question_id']}: {entry['scores']} "
              f"followup={entry['followup_asked']}")

    assert summary["state"] == "wrap", summary["state"]
    assert client._interviewer_frames > 0, "no interviewer audio received"
    assert stats["questions_asked"] >= 1, "no questions asked"
    assert summary["scores"], "no scores produced"
    assert "voice_budget_bar" in stats
    assert getattr(client, "ended", None) is not None, "no ended event"
    print("\nE2E PASSED (state=wrap, scores=%d)" % len(summary["scores"]))


if __name__ == "__main__":
    asyncio.run(main())
