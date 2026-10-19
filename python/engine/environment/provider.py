"""The :class:`Provider` base class: what a source must say and what the framework does.

A provider author writes only the *source-specific* parts:

* class attributes: ``id``, ``name``, ``kind`` (a provenance class), ``attribution``, ``licence``,
  ``homepage``, ``description``;
* :meth:`inventory`  -- what the source offers (for the picker in the UI);
* :meth:`plan`       -- which files a :class:`Request` needs (area/time/variables already applied:
  ask the server for the subset, never for more than needed), with sizes and checksums if the
  source publishes them;
* optionally :meth:`download_file` (default: a plain HTTP GET of ``spec.url``) and
  :meth:`provenance`.

Everything else is shared and tested once: the estimate shown before anything is downloaded, the
explicit approval, the cache (limit, LRU), checksums, retries and backoff, concurrency limits,
timeouts, offline mode, cancel and progress. See ``docs/ENVIRONMENT.md``.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from engine import layer_provenance
from engine.environment.errors import (
    ApprovalRequired,
    ChecksumMismatch,
    FetchCancelled,
    FetchFailed,
    OfflineError,
    RequestTooLarge,
)
from engine.environment.request import (
    Approval,
    Estimate,
    FetchResult,
    FileSpec,
    InventoryItem,
    Request,
)

if TYPE_CHECKING:
    from engine.environment.core import Environment

#: Called with ``{"percent", "message", "file", "bytes_done", "bytes_total"}``.
ProgressFn = Callable[[dict[str, Any]], None]
PLAN_TTL_S = 6 * 3600.0


class Provider:
    """Base class of every data source. Subclass, fill the attributes, implement :meth:`plan`."""

    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    #: Provenance class of what this provider serves (see ``engine.layer_provenance.CLASSES``).
    kind: ClassVar[str] = "observed"
    attribution: ClassVar[str] = ""
    licence: ClassVar[str] = ""
    homepage: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: False for sources that read files already on the disk (no download, no approval).
    requires_network: ClassVar[bool] = True
    #: Seconds a remembered plan (file listing) stays valid while online.
    plan_ttl_s: ClassVar[float] = PLAN_TTL_S
    #: Per file of unknown size: the most the download may transfer before it is stopped.
    unknown_size_cap: ClassVar[int] = 50 * 1024 * 1024

    # -- to implement -----------------------------------------------------------------------

    def inventory(self, query: dict[str, Any] | None = None) -> list[InventoryItem]:
        """The products on offer. Must not download data; a small listing request is fine."""
        return []

    def plan(self, request: Request, env: Environment) -> list[FileSpec]:  # pragma: no cover
        """The files *request* needs. May make small listing requests through ``env.http``."""
        raise NotImplementedError

    def download_file(
        self,
        spec: FileSpec,
        dest: Path,
        env: Environment,
        *,
        max_bytes: int | None,
        progress: Callable[[int, int | None], None] | None,
        cancel: threading.Event | None,
    ) -> None:
        """Write *spec* to *dest*. Default: HTTP GET of ``spec.url`` through the polite client."""
        if not spec.url:
            raise FetchFailed(f"{self.name}: {spec.name} has no URL to download from.")
        env.http.download(spec.url, dest, max_bytes=max_bytes, progress=progress, cancel=cancel)

    def local_path(self, spec: FileSpec) -> Path | None:
        """For providers that use files in place (no copy into the cache): the file's path."""
        return None

    def provenance(self, request: Request, specs: list[FileSpec]) -> dict[str, Any]:
        """The ``provenance`` dict of what was fetched (override for a precise method text)."""
        return layer_provenance.for_provider(
            kind=self.kind,
            source=self.name,
            attribution=self.attribution,
            licence=self.licence,
            request=request.to_dict(),
        ).to_dict()

    # -- shared machinery (do not override) -------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "attribution": self.attribution,
            "licence": self.licence,
            "homepage": self.homepage,
            "description": self.description,
            "requires_network": self.requires_network,
        }

    def _planned(self, request: Request, env: Environment) -> tuple[list[FileSpec], str]:
        """Plan with a remembered listing, so an estimate works offline once it was seen."""
        key = f"plan:{request.digest()}"
        cache = env.cache
        if self.requires_network:
            path = cache.lookup(self.id, key)
            if path is not None:
                try:
                    import json

                    blob = json.loads(path.read_text(encoding="utf-8"))
                    fresh = time.time() - float(blob.get("saved", 0)) < self.plan_ttl_s
                    if fresh or env.offline:
                        return [FileSpec.from_dict(d) for d in blob["files"]], "listing from cache"
                except (OSError, ValueError, KeyError):
                    pass
            if env.offline:
                return [], "offline and this request was never listed"
        specs = self.plan(request, env)
        if self.requires_network and specs:
            import json

            blob = {"saved": time.time(), "files": [s.to_dict() for s in specs]}
            with contextlib.suppress(OSError):
                cache.put_bytes(
                    self.id,
                    key,
                    json.dumps(blob).encode(),
                    name=f"plan-{request.digest()[:12]}.json",
                )
        return specs, ""

    def estimate(self, request: Request, env: Environment | None = None) -> Estimate:
        """What fetching *request* would download. Never downloads data."""
        from engine.environment.core import Environment as _Env

        env = env or _Env.default()
        offline = env.offline
        specs, note = self._planned(request, env)
        cached = [self._is_available(s, env) for s in specs]
        est = Estimate(
            request=request,
            provider_id=self.id,
            provider_name=self.name,
            files=specs,
            cached=cached,
            destination=str(env.cache.root),
            attribution=self.attribution,
            licence=self.licence,
            source=self.homepage or self.name,
            offline=offline,
            needs_network=self.requires_network,
            unknown_size_cap=self.unknown_size_cap,
        )
        if note:
            est.notes.append(note)
        if not specs:
            est.can_fetch = False
            est.reason = (
                "Offline mode is on and this request was never listed, so the files needed are "
                "not known."
                if offline and self.requires_network
                else "The source has nothing for this request."
            )
        elif self.requires_network:
            missing = est.network_files
            if offline and missing:
                est.can_fetch = False
                est.reason = (
                    f"Offline mode is on and {len(missing)} of {len(specs)} file(s) are not in "
                    "the cache. Switch offline mode off to download them."
                )
            elif est.total_bytes > env.cache.max_bytes:
                est.can_fetch = False
                est.reason = (
                    f"The request ({est.total_bytes / 1e6:.0f} MB) is larger than the cache limit "
                    f"({env.cache.max_bytes / 1e6:.0f} MB). Narrow the area, time or variables, "
                    "or raise the limit in the environment-data settings."
                )
        if not est.size_exact and est.network_files:
            est.notes.append(
                "Some sizes are not known in advance; the download stops at "
                f"{est.unknown_size_cap // (1024 * 1024)} MB per such file."
            )
        return est

    def _is_available(self, spec: FileSpec, env: Environment) -> bool:
        if self.local_path(spec) is not None:
            return True
        return env.cache.contains(self.id, spec.key)

    def fetch(
        self,
        request: Request,
        approval: Approval | None = None,
        env: Environment | None = None,
        *,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> FetchResult:
        """Fetch *request* into the cache. A download needs an :class:`Approval` of its estimate.

        Files already cached are used without network (so a cached request works offline and
        needs no approval). Raises :class:`ApprovalRequired`, :class:`OfflineError`,
        :class:`RequestTooLarge`, :class:`ChecksumMismatch`, :class:`FetchCancelled` or
        :class:`FetchFailed`; nothing incomplete stays in the cache.
        """
        from engine.environment.core import Environment as _Env

        env = env or _Env.default()
        est = self.estimate(request, env)
        if not est.can_fetch:
            if env.offline and self.requires_network and (est.network_files or not est.files):
                raise OfflineError(est.reason)
            raise RequestTooLarge(est.reason or "This request cannot be fetched.")
        if est.requires_approval and (
            approval is None or not approval.covers(request, est.max_network_bytes, est.destination)
        ):
            raise ApprovalRequired(
                "Nothing was downloaded: this fetch needs your approval of the estimate "
                f"({len(est.network_files)} file(s), about {est.network_bytes / 1e6:.1f} MB from "
                f"{est.source}, into {est.destination})."
            )
        cancel = cancel or threading.Event()
        keys = [s.key for s in est.files]
        todo = [s for s, c in zip(est.files, est.cached, strict=True) if not c]
        total = max(1, sum((s.size or 0) for s in todo))
        state = {"done": 0, "downloaded": 0}
        lock = threading.Lock()
        per_file: dict[str, int] = {}

        def emit(spec: FileSpec, message: str) -> None:
            if progress:
                pct = min(99, int(100 * state["done"] / total)) if todo else 100
                progress(
                    {
                        "percent": pct,
                        "message": message,
                        "file": spec.name,
                        "bytes_done": state["done"],
                        "bytes_total": total if est.size_exact else None,
                    }
                )

        def one(spec: FileSpec) -> None:
            cap = spec.size if spec.size is not None else est.unknown_size_cap
            for attempt in (0, 1):
                tmp = env.cache.tmp_path(spec.name)

                def on_bytes(done: int, _total: int | None, spec: FileSpec = spec) -> None:
                    with lock:
                        state["done"] += done - per_file.get(spec.key, 0)
                        per_file[spec.key] = done
                    emit(spec, f"Downloading {spec.name}")

                try:
                    per_file[spec.key] = 0
                    self.download_file(
                        spec, tmp, env, max_bytes=cap, progress=on_bytes, cancel=cancel
                    )
                    env.cache.commit(
                        self.id,
                        spec.key,
                        tmp,
                        name=spec.name,
                        expected_sha256=spec.sha256,
                        url=spec.url,
                        protect=keys,
                    )
                    with lock:
                        state["downloaded"] += per_file.get(spec.key, 0)
                    return
                except ChecksumMismatch:
                    with lock:
                        state["done"] -= per_file.get(spec.key, 0)
                    if attempt == 1:
                        raise  # a second corrupt copy: stop and say so
                except BaseException:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise

        if todo:
            workers = max(1, min(env.settings.max_concurrent, len(todo)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="env-fetch") as pool:
                futures = [pool.submit(one, s) for s in todo]
                try:
                    for fut in futures:
                        fut.result()
                except BaseException:
                    cancel.set()  # stop the other transfers; completed files stay cached
                    for fut in futures:
                        fut.cancel()
                    raise
        if cancel.is_set():
            raise FetchCancelled("cancelled")

        paths: list[Path] = []
        sums: dict[str, str] = {}
        for spec in est.files:
            local = self.local_path(spec)
            if local is not None:
                paths.append(local)
                continue
            p = env.cache.lookup(self.id, spec.key)
            if p is None:  # evicted by a concurrent fetch of something else
                raise FetchFailed(
                    f"{spec.name} was removed from the cache before it could be used. "
                    "Raise the cache limit in the environment-data settings."
                )
            paths.append(p)
            entry = env.cache.entry(self.id, spec.key) or {}
            if entry.get("sha256"):
                sums[spec.name] = entry["sha256"]
        if progress:
            progress(
                {
                    "percent": 100,
                    "message": "Done",
                    "file": "",
                    "bytes_done": state["done"],
                    "bytes_total": None,
                }
            )
        return FetchResult(
            request=request,
            paths=paths,
            from_cache=list(est.cached),
            bytes_downloaded=state["downloaded"],
            attribution=self.attribution,
            licence=self.licence,
            provenance=self.provenance(request, est.files),
            checksums=sums,
        )
