"""US-009 / US-010 — bank storage backends and the materialised cache.

The load-bearing test here is `test_glob_consumers_see_backend_changes`. The
whole design rests on one property: three consumers glob the banks folder
independently — `skills.discover_local_banks()`, `voice.agent.domain_from_room`
and `scripts/prepopulate_banks.sh` — and `agent.py` promises a skill uploaded
after a worker booted is recognised WITHOUT a restart. If authority moves to
object storage but the cache does not converge, that promise silently becomes
false and each replica serves a different set of skills.
"""
import asyncio
from pathlib import Path

import pytest

from interviewer import skills
from interviewer.bank_store import (CachedBankStore, LocalBankStore,
                                    build_bank_store)


BANK = b"# Q1\n\nExpected points: a, b\n\n## Question one\n\nbody\n"


class FakeBackend:
    """An in-memory BankStore standing in for S3. Proves CachedBankStore works
    against ANY backend, which is the point of the protocol."""

    def __init__(self, initial: dict[str, bytes] | None = None) -> None:
        self.objects = dict(initial or {})
        self.list_calls = 0
        self.fail = False

    async def list(self) -> list[str]:
        self.list_calls += 1
        if self.fail:
            raise ConnectionError("backend down")
        return sorted(self.objects)

    async def get(self, name: str) -> bytes | None:
        if self.fail:
            raise ConnectionError("backend down")
        return self.objects.get(name)

    async def put(self, name: str, data: bytes) -> None:
        self.objects[name] = data

    async def delete(self, name: str) -> None:
        self.objects.pop(name, None)


# ── LocalBankStore: the unchanged default ───────────────────────────────────

def test_local_round_trip(tmp_path: Path) -> None:
    store = LocalBankStore(tmp_path)

    async def main():
        await store.put("dsa", BANK)
        assert await store.get("dsa") == BANK
        assert await store.list() == ["dsa"]
        await store.delete("dsa")
        assert await store.get("dsa") is None
        assert await store.list() == []

    asyncio.run(main())


def test_local_store_leaves_no_temp_files(tmp_path: Path) -> None:
    """The write is tmp-then-rename so a concurrent reader never sees a
    truncated bank. The temp file must not survive the rename."""
    async def main():
        await LocalBankStore(tmp_path).put("dsa", BANK)

    asyncio.run(main())
    assert [p.name for p in tmp_path.iterdir()] == ["dsa.md"]


def test_local_store_follows_a_monkeypatched_bank_dir(tmp_path, monkeypatch) -> None:
    """The seam the existing upload tests depend on. Binding the root at
    construction would make those patches invisible and the store would write
    to the real repo folder while the test asserted against tmp_path."""
    monkeypatch.setattr(skills, "BANK_DIR", tmp_path)
    store = LocalBankStore()          # root=None -> resolved per call

    async def main():
        await store.put("dsa", BANK)

    asyncio.run(main())
    assert (tmp_path / "dsa.md").read_bytes() == BANK


# ── CachedBankStore: the property the design rests on ───────────────────────

def test_glob_consumers_see_backend_changes(tmp_path: Path) -> None:
    """**The acceptance criterion.** A bank that appears in the BACKEND --
    never written by this process, which is exactly the other-replica case --
    must become visible to the folder-globbing consumers after a refresh."""
    backend = FakeBackend()
    cache = CachedBankStore(backend, tmp_path)

    async def main():
        assert skills.discover_local_banks(tmp_path) == []

        # Another replica uploads. This process never calls put().
        await backend.put("uploaded-elsewhere", BANK)

        await cache.refresh(force=True)
        banks = skills.discover_local_banks(tmp_path)
        assert [b.name for b in banks] == ["uploaded-elsewhere"], (
            "a bank uploaded by another replica must reach the glob consumers "
            "or every replica serves a different set of skills"
        )

    asyncio.run(main())


def test_put_is_visible_to_this_replica_immediately(tmp_path: Path) -> None:
    """The uploader must never be told "saved" and then not see it. Writes go
    through to the cache without waiting for the refresh window."""
    backend = FakeBackend()
    cache = CachedBankStore(backend, tmp_path, refresh_s=3600)

    async def main():
        await cache.put("fresh", BANK)
        assert [b.name for b in skills.discover_local_banks(tmp_path)] == ["fresh"]

    asyncio.run(main())


def test_deleted_banks_are_removed_from_the_cache(tmp_path: Path) -> None:
    """Otherwise a deleted bank lives on in every replica's cache forever."""
    backend = FakeBackend({"gone": BANK})
    cache = CachedBankStore(backend, tmp_path)

    async def main():
        await cache.refresh(force=True)
        assert (tmp_path / "gone.md").exists()

        await backend.delete("gone")
        await cache.refresh(force=True)
        assert not (tmp_path / "gone.md").exists(), (
            "a deleted bank must not survive in the materialised cache"
        )

    asyncio.run(main())


def test_refresh_failure_serves_stale_not_empty(tmp_path: Path) -> None:
    """A backend blip must not present to a candidate as an interview with no
    questions. Stale banks are strictly better than none."""
    backend = FakeBackend({"dsa": BANK})
    cache = CachedBankStore(backend, tmp_path)

    async def main():
        await cache.refresh(force=True)
        assert (tmp_path / "dsa.md").exists()

        backend.fail = True
        await cache.refresh(force=True)

        assert (tmp_path / "dsa.md").exists(), (
            "a refresh failure must leave the cache intact"
        )
        assert [b.name for b in skills.discover_local_banks(tmp_path)] == ["dsa"]

    asyncio.run(main())


def test_refresh_is_rate_limited_unless_forced(tmp_path: Path) -> None:
    """The TTL is what bounds LIST traffic; force=True is for tests and
    startup."""
    backend = FakeBackend({"dsa": BANK})
    cache = CachedBankStore(backend, tmp_path, refresh_s=3600)

    async def main():
        await cache.refresh()
        after_first = backend.list_calls
        await cache.refresh()
        assert backend.list_calls == after_first, "TTL was not honoured"

    asyncio.run(main())


# ── backend selection ───────────────────────────────────────────────────────

def test_local_config_selects_local_and_reads_the_same_dir(tmp_path) -> None:
    """For local, store and read-dir are the same folder -- nothing moves."""
    from interviewer.config import InterviewerConfig
    store, read_dir = build_bank_store(InterviewerConfig.from_env({}), tmp_path)
    assert isinstance(store, LocalBankStore)
    assert read_dir == tmp_path


def test_s3_config_wraps_in_a_cache(tmp_path) -> None:
    from interviewer.config import InterviewerConfig
    cfg = InterviewerConfig.from_env({
        "INTERVIEW_BANK_STORE": "s3",
        "INTERVIEW_BANK_S3_BUCKET": "banks",
    })
    store, read_dir = build_bank_store(cfg, tmp_path)
    assert isinstance(store, CachedBankStore)
    assert read_dir == tmp_path, (
        "readers must keep pointing at the materialised cache"
    )


def test_unknown_backend_fails_loudly(tmp_path) -> None:
    from interviewer.config import InterviewerConfig
    cfg = InterviewerConfig.from_env({"INTERVIEW_BANK_STORE": "dropbox"})
    with pytest.raises(ValueError, match="unknown INTERVIEW_BANK_STORE"):
        build_bank_store(cfg, tmp_path)
