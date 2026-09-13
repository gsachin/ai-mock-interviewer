"""US-007 — CORS origins come from config.

The failure mode this guards is quiet: with a hardcoded localhost allowlist,
serving from a real hostname produces a browser-side CORS error against a
perfectly healthy server. Nothing in the logs, nothing in the probes, and the
page simply does not talk to the API.
"""
import pytest
from fastapi.testclient import TestClient

from interviewer import server as srv
from interviewer.config import InterviewerConfig, _as_origins


@pytest.fixture()
def client():
    return TestClient(srv.app)


def test_defaults_are_the_previously_hardcoded_values() -> None:
    """The default must not change behaviour for the Windows dev flow."""
    cfg = InterviewerConfig.from_env({})
    assert cfg.cors_origins == (
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:8501", "http://127.0.0.1:8501",
    )


def test_an_allowed_origin_is_echoed_back(client) -> None:
    resp = client.get("/health", headers={"Origin": "http://localhost:8501"})
    assert resp.headers.get("access-control-allow-origin") == \
        "http://localhost:8501"


def test_an_unlisted_origin_is_refused(client) -> None:
    """The regression: a deployed hostname must not silently get no CORS
    header, which is what the hardcoded list produced."""
    resp = client.get("/health", headers={"Origin": "https://interview.example.com"})
    assert "access-control-allow-origin" not in resp.headers


def test_origins_are_overridable() -> None:
    cfg = InterviewerConfig.from_env({
        "INTERVIEW_CORS_ORIGINS": "https://a.example.com, https://b.example.com",
    })
    assert cfg.cors_origins == ("https://a.example.com", "https://b.example.com")


def test_empty_means_no_cors_and_is_not_treated_as_unset() -> None:
    """An empty value must be respected, not fall back to the defaults.

    Conflating "" with unset would make CORS impossible to turn OFF -- and off
    is what a same-origin ingress deployment wants, since the UI is served by
    this same app.
    """
    assert _as_origins("") == ()
    assert _as_origins(None) == InterviewerConfig.from_env({}).cors_origins


def test_wildcard_origin_is_passed_through_verbatim() -> None:
    """`*` must survive: stripping or quoting it would break a deliberate
    wide-open dev configuration."""
    assert _as_origins("*") == ("*",)
