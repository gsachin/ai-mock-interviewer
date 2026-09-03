"""Shared fixtures for the enterprise-rag-core test suite."""
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / "models" / "reranker" / "minilm-int8.onnx"

# Repo-known xet.dll issue on Windows when huggingface_hub downloads models.
os.environ.setdefault("HF_HUB_ENABLE_HF_XET", "0")


@pytest.fixture(scope="session")
def reranker_model_path() -> Path | None:
    """Path to the INT8 MiniLM ONNX model, or None (tests skip) if not downloaded."""
    return MODEL_PATH if MODEL_PATH.exists() else None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def redis_stack_url() -> str | None:
    """Redis Stack URL when reachable, else None (redis-marked tests skip)."""
    url = os.environ.get("REDIS_STACK_URL", "redis://localhost:6379")
    host = url.split("//")[1].split(":")[0]
    port = int(url.rsplit(":", 1)[1])
    return url if _port_open(host, port) else None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class RunningApp:
    """Boot an ASGI app via uvicorn in a background thread; yields the base URL."""

    def __init__(self, app):
        import uvicorn

        self.port = free_port()
        self.config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.srv = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.srv.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with httpx.Client(timeout=1.0) as c:
                    c.get(f"http://127.0.0.1:{self.port}/mcp")
                break
            except Exception:
                time.sleep(0.25)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *a):
        self.srv.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def running_app():
    """Context-manager factory: ``with running_app(app) as base_url:``.

    Module-scoped because booting a second uvicorn thread in the same process
    races on Windows; consumers that need per-test control can use RunningApp
    directly."""
    return RunningApp
