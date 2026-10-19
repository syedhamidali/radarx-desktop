"""On-disk cache of fetched environment files: size limit, LRU eviction, integrity records.

Layout under the cache root::

    index.json                       # key -> size, sha256, times, source (atomic rewrite)
    data/<provider>/<hh>/<hash>-<name>
    tmp/                             # partial downloads (never listed in the index)

A file only enters the index through :meth:`Cache.commit`, i.e. once it is complete (and, when the
source published a checksum, verified). When the total size exceeds the limit the least recently
*used* files are removed first, except those of the request being served.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from engine.environment.errors import ChecksumMismatch

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
INDEX_NAME = "index.json"


def safe_name(text: str, limit: int = 80) -> str:
    """A file-system-safe version of *text* (no path separators, no leading dots)."""
    name = _SAFE.sub("_", text).strip("._") or "file"
    return name[-limit:]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


class Cache:
    """A size-limited LRU cache. Thread-safe within a process."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        max_bytes: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).expanduser()
        self.max_bytes = int(max_bytes)
        self._clock = clock
        self._lock = threading.RLock()
        self._index: dict[str, dict[str, Any]] | None = None
        self._seq = 0

    # -- paths ----------------------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def tmp_path(self, hint: str = "dl") -> Path:
        """A fresh path for a partial download (inside the cache, so the final move is atomic)."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        return self.tmp_dir / f"{safe_name(hint, 40)}.{os.getpid()}.{threading.get_ident()}.part"

    @staticmethod
    def _id(provider: str, key: str) -> str:
        return f"{provider}\x1f{key}"

    # -- index ----------------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._index is not None:
            return self._index
        index: dict[str, dict[str, Any]] = {}
        path = self.root / INDEX_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            for eid, e in entries.items():
                if isinstance(e, dict) and isinstance(e.get("path"), str):
                    f = self.root / e["path"]
                    # drop records whose file vanished or was truncated behind our back
                    if f.is_file() and f.stat().st_size == e.get("size"):
                        index[eid] = e
        except (OSError, ValueError):
            index = {}
        self._seq = max((int(e.get("seq", 0)) for e in index.values()), default=0)
        self._index = index
        return index

    def _save(self) -> None:
        if self._index is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f"{INDEX_NAME}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"version": 1, "entries": self._index}), encoding="utf-8")
        os.replace(tmp, self.root / INDEX_NAME)

    def _touch(self, entry: dict[str, Any]) -> None:
        self._seq += 1
        entry["seq"] = self._seq
        entry["accessed"] = self._clock()

    # -- queries --------------------------------------------------------------------------

    def entry(self, provider: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            e = self._load().get(self._id(provider, key))
            return dict(e) if e else None

    def contains(self, provider: str, key: str) -> bool:
        """True when a complete, intact-looking file is cached (does not count as a use)."""
        with self._lock:
            e = self._load().get(self._id(provider, key))
            return bool(e) and (self.root / e["path"]).is_file()

    def lookup(self, provider: str, key: str, *, verify: bool = False) -> Path | None:
        """The cached file for *key* (counts as a use), or ``None``.

        With ``verify=True`` the content is re-hashed; a damaged entry is dropped and ``None`` is
        returned so the caller fetches it again.
        """
        with self._lock:
            index = self._load()
            eid = self._id(provider, key)
            e = index.get(eid)
            if not e:
                return None
            path = self.root / e["path"]
            if not path.is_file() or path.stat().st_size != e.get("size"):
                self._drop(eid)
                self._save()
                return None
            if verify and e.get("sha256") and sha256_file(path) != e["sha256"]:
                self._drop(eid)
                self._save()
                return None
            self._touch(e)
            self._save()
            return path

    def usage(self) -> dict[str, Any]:
        with self._lock:
            index = self._load()
            return {
                "root": str(self.root),
                "bytes": sum(int(e.get("size", 0)) for e in index.values()),
                "files": len(index),
                "max_bytes": self.max_bytes,
            }

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e, id=i) for i, e in self._load().items()]

    # -- changes --------------------------------------------------------------------------

    def commit(
        self,
        provider: str,
        key: str,
        tmp_file: Path,
        *,
        name: str,
        expected_sha256: str | None = None,
        url: str | None = None,
        protect: Iterable[str] = (),
    ) -> tuple[Path, str]:
        """Move a finished download into the cache; return ``(path, sha256)``.

        Raises :class:`ChecksumMismatch` (and deletes the file) when *expected_sha256* differs.
        """
        digest = sha256_file(tmp_file)
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            with contextlib.suppress(OSError):
                tmp_file.unlink()
            raise ChecksumMismatch(
                f"{name}: the downloaded file does not match its published checksum "
                f"(expected {expected_sha256[:12]}..., got {digest[:12]}...); it was discarded."
            )
        size = tmp_file.stat().st_size
        h = hashlib.sha1(self._id(provider, key).encode()).hexdigest()
        rel = Path("data") / safe_name(provider, 40) / h[:2] / f"{h[:16]}-{safe_name(name)}"
        dest = self.root / rel
        with self._lock:
            index = self._load()
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_file, dest)
            eid = self._id(provider, key)
            old = index.get(eid)
            if old and old["path"] != rel.as_posix():
                with contextlib.suppress(OSError):
                    (self.root / old["path"]).unlink()
            entry = {
                "provider": provider,
                "key": key,
                "name": name,
                "path": rel.as_posix(),
                "size": size,
                "sha256": digest,
                "url": url,
                "created": self._clock(),
            }
            self._touch(entry)
            index[eid] = entry
            self.evict(protect={eid, *(self._id(provider, k) for k in protect)})
            self._save()
        return dest, digest

    def put_bytes(
        self, provider: str, key: str, data: bytes, *, name: str, protect: Iterable[str] = ()
    ) -> Path:
        tmp = self.tmp_path(name)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        return self.commit(provider, key, tmp, name=name, protect=protect)[0]

    def _drop(self, eid: str) -> int:
        e = self._load().pop(eid, None)
        if not e:
            return 0
        with contextlib.suppress(OSError):
            (self.root / e["path"]).unlink()
        return int(e.get("size", 0))

    def evict(self, to_bytes: int | None = None, protect: Iterable[str] = ()) -> list[str]:
        """Remove least-recently-used files until the cache fits *to_bytes* (default: the limit).

        Entries whose ids are in *protect* are kept. Returns the evicted keys.
        """
        limit = self.max_bytes if to_bytes is None else int(to_bytes)
        keep = set(protect)
        removed: list[str] = []
        with self._lock:
            index = self._load()
            total = sum(int(e.get("size", 0)) for e in index.values())
            if total <= limit:
                return removed
            for eid, e in sorted(index.items(), key=lambda kv: kv[1].get("seq", 0)):
                if total <= limit:
                    break
                if eid in keep:
                    continue
                total -= self._drop(eid)
                removed.append(e.get("key", eid))
            self._save()
        return removed

    def remove(self, provider: str, key: str) -> bool:
        with self._lock:
            eid = self._id(provider, key)
            existed = eid in self._load()
            self._drop(eid)
            self._save()
            return existed

    def clear(self, provider: str | None = None) -> int:
        """Delete every cached file (of one provider, or all). Returns the bytes freed."""
        with self._lock:
            index = self._load()
            freed = 0
            for eid in [i for i, e in index.items() if provider in (None, e.get("provider"))]:
                freed += self._drop(eid)
            if provider is None:
                shutil.rmtree(self.tmp_dir, ignore_errors=True)
            self._save()
            return freed

    def sweep_partials(self, max_age_s: float = 3600.0) -> int:
        """Delete stale partial downloads left by a crash. Returns how many were removed."""
        n = 0
        now = time.time()
        if not self.tmp_dir.is_dir():
            return 0
        for p in self.tmp_dir.glob("*.part"):
            try:
                if now - p.stat().st_mtime > max_age_s:
                    p.unlink()
                    n += 1
            except OSError:
                continue
        return n
