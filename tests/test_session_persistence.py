"""US-005 (session serialization) and US-004 (sessions in a shared store).

The acceptance criterion for US-004 is not "the endpoints still work" — they
worked before, on a process-local dict. It is that **any replica can serve a
session created on any other replica**, which these tests pin by reading
through the store abstraction rather than through the app's memory.
"""
import json

import pytest
from fastapi.testclient import TestClient

from interviewer import server as srv
from interviewer.session_store import InMemorySessionStore
from interviewer.state_machine import InterviewerState, Session, Turn


# ── US-005: the serializer ──────────────────────────────────────────────────

def _populated() -> Session:
    return Session(
        session_id="abc123def456",
        tenant_id="acme",
        domain="dsa",
        state=InterviewerState.EVALUATE,
        current_question_id="q-3",
        turns=[Turn(role="interviewer", text="Explain a hash map."),
               Turn(role="candidate", text="It maps keys to buckets.")],
        scores=[{"question_id": "q-1", "correctness": 4}],
    )


def test_to_dict_is_json_serializable() -> None:
    """The actual US-005 failure. `RedisSessionStore.save` calls json.dumps,
    which refuses dataclasses — so this is the assertion that would have
    raised TypeError before the serializer existed."""
    payload = json.dumps(_populated().to_dict())
    assert "abc123def456" in payload


def test_round_trip_preserves_every_field() -> None:
    original = _populated()
    restored = Session.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.session_id == original.session_id
    assert restored.tenant_id == original.tenant_id
    assert restored.domain == original.domain
    assert restored.state is InterviewerState.EVALUATE
    assert restored.current_question_id == "q-3"
    assert [(t.role, t.text) for t in restored.turns] == [
        ("interviewer", "Explain a hash map."),
        ("candidate", "It maps keys to buckets."),
    ]
    assert restored.scores == original.scores


def test_state_comes_back_as_the_enum_not_a_string() -> None:
    """A str-Enum round-trips to a plain str through JSON. If from_dict did
    not re-coerce it, `session.state.value` would still work but
    `session.transition(...)` would silently fail to find its transition."""
    restored = Session.from_dict(_populated().to_dict())
    assert isinstance(restored.state, InterviewerState)
    restored.transition  # bound and callable
    assert (restored.state, ) == (InterviewerState.EVALUATE,)


def test_unknown_keys_are_ignored_for_rolling_deploys() -> None:
    """An old replica must be able to read a record written by a newer one.
    Rejecting unknown keys would turn a schema addition into an outage."""
    data = _populated().to_dict()
    data["added_in_a_later_release"] = {"nested": True}
    data["turns"].append({"role": "candidate", "text": "extra",
                          "new_field": 1})

    restored = Session.from_dict(data)
    assert restored.session_id == "abc123def456"
    assert len(restored.turns) == 3


def test_unknown_state_raises_rather_than_defaulting() -> None:
    """The one thing from_dict must NOT tolerate. Silently falling back to
    GREETING would rewind a live interview to its first question — worse than
    a loud failure."""
    data = _populated().to_dict()
    data["state"] = "not_a_real_state"

    with pytest.raises(ValueError, match="unknown state"):
        Session.from_dict(data)


def test_missing_optional_fields_use_defaults() -> None:
    restored = Session.from_dict({"session_id": "min", "domain": "dsa"})
    assert restored.state is InterviewerState.GREETING
    assert restored.tenant_id == "default"
    assert restored.turns == []
    assert restored.scores == []


# ── US-004: sessions behind the store ───────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    """A shared store swapped in at the `_store` seam, matching how the
    existing tests swap `server._rag`."""
    store = InMemorySessionStore()
    monkeypatch.setattr(srv, "_store", store)
    return TestClient(srv.app), store


def test_session_survives_a_read_that_never_touched_this_process(client) -> None:
    """**The US-004 acceptance criterion.** The session is written straight
    into the store — never through the app — and then fetched over HTTP. That
    is precisely the replica-B case: a store entry this process did not
    create. Against the old module-level dict this returns 404."""
    http, store = client

    import anyio
    anyio.run(store.save, "replica-a-session",
              _populated().to_dict())

    resp = http.get("/sessions/replica-a-session")
    assert resp.status_code == 200, (
        "GET /sessions/{id} must read the store, not process memory — "
        "otherwise replica B 404s on replica A's session"
    )
    body = resp.json()
    assert body["session_id"] == "abc123def456"
    assert body["state"] == "evaluate"
    assert len(body["turns"]) == 2
    assert body["scores"] == [{"question_id": "q-1", "correctness": 4}]


def test_create_then_get_round_trips_through_the_store(client) -> None:
    http, store = client

    created = http.post("/sessions", json={"tenant_id": "acme", "domain": "dsa"})
    assert created.status_code == 200
    sid = created.json()["session_id"]

    # It must be IN the store, not just returned to the caller.
    import anyio
    stored = anyio.run(store.load, sid)
    assert stored is not None, "POST /sessions did not persist to the store"
    assert stored["domain"] == "dsa"

    fetched = http.get(f"/sessions/{sid}")
    assert fetched.status_code == 200
    assert fetched.json()["tenant_id"] == "acme"


def test_unknown_session_is_404_not_500(client) -> None:
    http, _ = client
    assert http.get("/sessions/does-not-exist").status_code == 404


def test_default_store_is_still_memory() -> None:
    """The Windows dev flow and the existing tests depend on this default.
    Flipping it would make an unconfigured `start_services.ps1` run require
    Redis."""
    from interviewer.config import InterviewerConfig
    assert InterviewerConfig.from_env({}).session_store == "memory"


def test_redis_store_is_selected_by_config(monkeypatch) -> None:
    """The wiring, without needing a live Redis: the right class is chosen."""
    from interviewer.session_store import RedisSessionStore as R
    monkeypatch.setenv("INTERVIEW_SESSION_STORE", "redis")
    monkeypatch.setenv("INTERVIEW_REDIS_URL", "redis://example:6379")

    from interviewer.config import InterviewerConfig
    cfg = InterviewerConfig.from_env()
    assert cfg.session_store == "redis"
    assert isinstance(R(cfg.redis_url), R)
