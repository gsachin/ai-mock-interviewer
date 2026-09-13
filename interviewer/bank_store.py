"""Question-bank storage (US-009 / US-010).

Before this, ``POST /skills`` wrote a file to the pod's own disk and every
request re-globbed that folder. With more than one replica that is silently
wrong in both directions: an upload on replica A is invisible to replica B,
and each worker's ``domain_from_room`` sees a different set of skills.

The design constraint that shapes everything here is **the filesystem API
stays**. Three consumers glob the banks folder independently — ``skills
.discover_local_banks()``, ``voice.agent.domain_from_room`` and
``scripts/prepopulate_banks.sh`` — and ``agent.py`` explicitly promises that a
skill uploaded after a worker booted is recognised *without a restart*. So
rather than converting call sites to a store API, the store materialises into
a local directory and ``skills.bank_dir()`` points there. Authority moves; the
reading code does not.

Why object storage rather than a ReadWriteMany volume: RWX is the least
portable thing in Kubernetes (NFS/CephFS/EFS/Azure Files each need their own
provisioner, on-prem needs an NFS server) and would put a cloud-specific
dependency in the base, which the cloud-agnostic + on-prem requirement rules
out. The S3 API is the portable abstraction — MinIO on-prem, any cloud object
store, or LocalBankStore for a single node.
"""
import os
import time
from pathlib import Path
from typing import Protocol

# How long a materialised cache is trusted before re-listing the authoritative
# store. Short on purpose: it is the only thing keeping the "uploaded after
# boot, visible without a restart" promise true across replicas. A larger value
# trades that promise for fewer LIST calls; a bank upload is a rare, human
# action, so the LIST traffic is negligible and the staleness window is what
# actually matters.
DEFAULT_REFRESH_S = 10.0


class BankStore(Protocol):
    """Where banks authoritatively live."""

    async def list(self) -> list[str]:
        """Bank names (file stems), sorted."""
        ...

    async def get(self, name: str) -> bytes | None:
        """Bank markdown, or None if absent."""
        ...

    async def put(self, name: str, data: bytes) -> None:
        """Create or replace."""
        ...

    async def delete(self, name: str) -> None:
        ...


class LocalBankStore:
    """The banks folder itself — the pre-US-009 behaviour, unchanged.

    This is the default (``INTERVIEW_BANK_STORE=local``) so the Windows dev
    flow and single-node on-prem installs behave exactly as before, and so the
    existing tests that monkeypatch ``skills.BANK_DIR`` keep working.
    """

    def __init__(self, root: Path | None = None) -> None:
        # None means "resolve on every call via skills.bank_dir()". That is not
        # laziness for its own sake: BANK_DIR is a module attribute tests
        # monkeypatch, and binding the path once at construction would make
        # those patches invisible -- the store would write to the real repo
        # folder while the test asserted against a tmp_path. Same seam lesson
        # as server._store and server._rag.
        self._root = root

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        from interviewer import skills   # leaf module, no cycle
        return skills.bank_dir()

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.md"

    async def list(self) -> list[str]:
        root = self.root
        if not root.is_dir():
            return []
        return sorted(p.stem for p in root.glob("*.md"))

    async def get(self, name: str) -> bytes | None:
        path = self._path(name)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    async def put(self, name: str, data: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader who globs mid-write must never see a
        # truncated bank and start an interview from half a question list.
        tmp = self._path(name).with_suffix(".md.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self._path(name))

    async def delete(self, name: str) -> None:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass


class S3BankStore:
    """S3-compatible object storage (MinIO, AWS S3, GCS via gateway).

    ``aioboto3`` is imported lazily, exactly as ``RedisSessionStore`` lazily
    imports ``redis``: the package must remain importable and fully functional
    on the local/memory path without the extra installed. If you configure
    ``INTERVIEW_BANK_STORE=s3`` and the extra is missing, you get a clear
    error on first use rather than on import.
    """

    def __init__(self, bucket: str, *, prefix: str = "question_banks/",
                 endpoint_url: str | None = None,
                 region: str | None = None,
                 access_key: str | None = None,
                 secret_key: str | None = None) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._client_cm = None
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                import aioboto3  # the [s3] extra
            except ImportError as exc:
                raise RuntimeError(
                    "INTERVIEW_BANK_STORE=s3 needs the optional dependency: "
                    "pip install -e '.[s3]' (aioboto3)"
                ) from exc
            session = aioboto3.Session()
            kwargs: dict = {}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            if self._region:
                kwargs["region_name"] = self._region
            if self._access_key:
                kwargs["aws_access_key_id"] = self._access_key
                kwargs["aws_secret_access_key"] = self._secret_key
            self._client_cm = session.client("s3", **kwargs)
            self._client = await self._client_cm.__aenter__()
        return self._client

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}.md"

    async def list(self) -> list[str]:
        client = await self._get_client()
        names: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._bucket,
                                             Prefix=self._prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".md"):
                    names.append(Path(key).stem)
        return sorted(names)

    async def get(self, name: str) -> bytes | None:
        client = await self._get_client()
        try:
            resp = await client.get_object(Bucket=self._bucket,
                                           Key=self._key(name))
        except Exception as exc:  # botocore ClientError on 404
            if "NoSuchKey" in str(exc) or "404" in str(exc):
                return None
            raise
        return await resp["Body"].read()

    async def put(self, name: str, data: bytes) -> None:
        client = await self._get_client()
        await client.put_object(Bucket=self._bucket, Key=self._key(name),
                                Body=data, ContentType="text/markdown")

    async def delete(self, name: str) -> None:
        client = await self._get_client()
        await client.delete_object(Bucket=self._bucket, Key=self._key(name))


class CachedBankStore:
    """Wraps any BankStore with a local materialised copy.

    This is what keeps the three glob consumers working unchanged: the
    authoritative bytes live in object storage, but they are mirrored into a
    local directory that ``skills.bank_dir()`` points at, so
    ``discover_local_banks`` and ``domain_from_room`` keep reading plain files.

    Writes go through to the backend AND into the cache immediately, so the
    replica that served the upload sees it with no delay at all; other replicas
    pick it up on the next refresh. That asymmetry is deliberate — the uploader
    should never be told "saved" and then not see it.

    On a refresh failure the cache is served as-is (stale) rather than emptied:
    a RAG or S3 blip must not turn every replica into "zero skills", which
    would present to a candidate as an interview with no questions.
    """

    def __init__(self, backend: BankStore, cache_dir: Path, *,
                 refresh_s: float = DEFAULT_REFRESH_S) -> None:
        self._backend = backend
        self._cache = cache_dir
        self._refresh_s = refresh_s
        self._last_refresh = 0.0

    @property
    def cache_dir(self) -> Path:
        return self._cache

    def _path(self, name: str) -> Path:
        return self._cache / f"{name}.md"

    def _write_local(self, name: str, data: bytes) -> None:
        self._cache.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name).with_suffix(".md.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self._path(name))

    async def refresh(self, *, force: bool = False) -> int:
        """Mirror the backend into the local cache. Returns banks cached."""
        now = time.monotonic()
        if not force and (now - self._last_refresh) < self._refresh_s:
            return len(list(self._cache.glob("*.md"))) if self._cache.is_dir() else 0
        try:
            names = await self._backend.list()
            self._cache.mkdir(parents=True, exist_ok=True)
            for name in names:
                data = await self._backend.get(name)
                if data is not None:
                    self._write_local(name, data)
            # Drop locals the backend no longer has, or a deleted bank would
            # live on in every replica's cache forever.
            for stale in self._cache.glob("*.md"):
                if stale.stem not in names:
                    stale.unlink()
            self._last_refresh = now
            return len(names)
        except Exception:
            # Serve stale rather than empty -- see the class docstring.
            return len(list(self._cache.glob("*.md"))) if self._cache.is_dir() else 0

    async def list(self) -> list[str]:
        await self.refresh()
        return await self._backend.list()

    async def get(self, name: str) -> bytes | None:
        local = self._path(name)
        if local.is_file():
            return local.read_bytes()
        return await self._backend.get(name)

    async def put(self, name: str, data: bytes) -> None:
        await self._backend.put(name, data)
        # Through to the cache immediately: the uploader must see its own
        # write without waiting for the refresh window.
        self._write_local(name, data)

    async def delete(self, name: str) -> None:
        await self._backend.delete(name)
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass


def build_bank_store(config, bank_dir: Path) -> tuple[BankStore, Path]:
    """Resolve the configured store and the directory readers should use.

    Returns ``(store, read_dir)``. For ``local`` they are the same folder and
    nothing changes; for ``s3`` the store is authoritative and ``read_dir`` is
    the materialised cache.
    """
    kind = (getattr(config, "bank_store", "local") or "local").lower()
    if kind == "local":
        # root=None so the store follows skills.bank_dir() dynamically.
        return LocalBankStore(), bank_dir
    if kind == "s3":
        backend = S3BankStore(
            bucket=config.bank_s3_bucket or "",
            prefix=config.bank_s3_prefix or "question_banks/",
            endpoint_url=config.bank_s3_endpoint_url,
            region=config.bank_s3_region,
            access_key=config.bank_s3_access_key,
            secret_key=config.bank_s3_secret_key,
        )
        cached = CachedBankStore(backend, bank_dir)
        return cached, bank_dir
    raise ValueError(
        f"unknown INTERVIEW_BANK_STORE {kind!r}; expected 'local' or 's3'")
