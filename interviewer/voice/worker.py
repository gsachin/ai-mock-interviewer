"""LiveKit agent worker entry point.

    python -m interviewer.voice.worker

Registers the interviewer agent with the LiveKit server (dev mode:
``livekit-server --dev`` on :7880, key ``devkey`` / secret ``secret``);
rooms created through ``POST /voice/token`` dispatch a job here.

Fail-fast: refuses to start unless a real STT/TTS pair resolves and the
hot-path voice LLM is configured — silent stubs never ship to a room.
"""
import logging

from livekit import agents

from interviewer.config import InterviewerConfig
from interviewer.voice import AGENT_NAME
from interviewer.voice.agent import run_agent
from interviewer.voice.stt import resolve_stt
from interviewer.voice.tts import resolve_tts

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    config = InterviewerConfig.from_env()

    if config.stt_provider in ("", "stub") or config.tts_provider in ("", "stub"):
        raise SystemExit(
            "voice worker requires real engines: set "
            "INTERVIEW_STT_PROVIDER=faster-whisper and "
            "INTERVIEW_TTS_PROVIDER=kokoro (stub engines are for tests)")
    resolve_stt(config.stt_provider, config)  # raises on missing credentials
    resolve_tts(config.tts_provider, config)  # raises on missing credentials
    if not config.voice_llm_base_url and not config.voice_llm_model:
        raise SystemExit(
            "INTERVIEW_VOICE_LLM_BASE_URL/INTERVIEW_VOICE_LLM_MODEL are "
            "required for the voice hot path (e.g. Ollama llama3.2:3b at "
            "http://127.0.0.1:11434/v1)")

    log.info("worker starting: stt=%s tts=%s voice_llm=%s livekit=%s agent=%s",
             config.stt_provider, config.tts_provider,
             config.voice_llm_model or config.voice_llm_base_url,
             config.livekit_url, AGENT_NAME)
    async def request_fnc(ctx):
        log.info("job request: type=%s agent=%s room=%s", ctx.job.type,
                 ctx.agent_name, ctx.job.room.name if ctx.job.room else "?")
        await ctx.accept()

    options = agents.WorkerOptions(
        entrypoint_fnc=run_agent,
        request_fnc=request_fnc,
        agent_name=AGENT_NAME,
        api_key=config.livekit_api_key or "devkey",
        api_secret=config.livekit_api_secret or "secret",
        ws_url=config.livekit_url.replace("http", "ws"),
        # Always accept jobs: the CPU-load gate (default threshold 0.7) marks
        # the worker unavailable during TTS/STT bursts on a busy dev machine,
        # and the server then refuses to dispatch at all.
        load_threshold=float("inf"),
    )
    server = agents.AgentServer.from_server_options(options)
    _orig_is_available = server._is_available

    def _log_unavailable() -> bool:
        """Always-accept worker: log the reason on the rare refusal so a
        dead dispatch is diagnosable from the worker log alone."""
        result = _orig_is_available()
        if not result:
            log.warning("refusing job: draining=%s devmode=%s load=%s "
                        "threshold=%s", server.draining, server._devmode,
                        getattr(server, "_worker_load", "?"),
                        server._load_threshold)
        return result

    server._is_available = _log_unavailable  # type: ignore[method-assign]
    agents.cli.run_app(server)


if __name__ == "__main__":
    import sys

    if len(sys.argv) <= 1:  # no subcommand -> registered worker mode
        # NOTE: the legacy ``dev`` subcommand makes the worker answer job
        # availability with "unavailable" on this stack (verified against
        # livekit-server 1.13.6); ``start`` is the working mode.
        sys.argv.append("start")
    main()
