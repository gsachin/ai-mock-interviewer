"""Logging configuration — one JSON object per line on stdout, or the plain
text format the Windows dev flow already produces.

``LOG_FORMAT=text`` is the **default** so the existing developer experience is
byte-identical: the format string below reproduces exactly what
``voice/worker.py`` has always configured, and what the launcher's
``$env:TEMP\\interviewer_*.log`` files contain today. ``LOG_FORMAT=json`` is
what the cluster sets, because a log shipper wants one object per line.

Correlation ids (``session_id``, ``room``, ``request_id``) travel in
``contextvars`` rather than being threaded through call signatures. The
existing log lines already embed ``session_id``/``room`` inside their message
text, so this is an upgrade of what is there, not a rewrite of every call site.
"""
import contextvars
import json
import logging
import os
import sys
import time

# Set by the API middleware and by the worker's job entrypoint; read by the
# formatter. ContextVars are per-task, so concurrent interviews in one worker
# process never see each other's ids.
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default="")
room_var: contextvars.ContextVar[str] = contextvars.ContextVar("room", default="")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="")

# Exactly the format worker.py has always used — do not "tidy" this, its whole
# purpose is that text-mode output is unchanged.
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Loggers that are noisy at INFO and tell us nothing we act on.
_QUIET = {"httpx": logging.WARNING, "httpcore": logging.WARNING}


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields on the record survive, so
    ``log.info("...", extra={"hop": "tts_first_audio"})`` carries through."""

    # Attributes LogRecord always has; anything else on the record was added
    # by the caller and is worth emitting. color_message is uvicorn's
    # ANSI-coloured duplicate of msg — emitting it would put raw escape codes
    # into a log pipeline that has no terminal behind it.
    _RESERVED = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    ) | {"message", "asctime", "taskName", "color_message"}

    def __init__(self, service: str = "") -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self._service:
            payload["service"] = self._service
        for name, var in (("session_id", session_id_var),
                          ("room", room_var),
                          ("request_id", request_id_var)):
            value = var.get()
            if value:
                payload[name] = value
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # ensure_ascii=False keeps non-ASCII transcript text readable rather
        # than escaped, which matters for a product whose payload is speech.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service: str = "", level: str | None = None) -> None:
    """Install the formatter on the root logger.

    ``service`` names the emitting component in JSON mode (``api``,
    ``voice-worker``, ``rag``). ``level`` falls back to ``LOG_LEVEL``.
    Idempotent: calling it twice (e.g. server import then a test) replaces the
    handler rather than stacking a second one.
    """
    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)

    for name, quiet_level in _QUIET.items():
        logging.getLogger(name).setLevel(quiet_level)

    # uvicorn installs its OWN handlers on these loggers and does not
    # propagate, so configuring only the root logger leaves the server's own
    # lines in plain text while the app's lines are JSON — a mixed stream that
    # a log shipper parses badly. Routing them through the root handler gives
    # one consistent format on stdout. `--log-config` is deliberately not used
    # instead: it would need a JSON file mounted into the image, and this keeps
    # the format decision in one place for both entrypoints.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
