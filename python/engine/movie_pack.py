"""The optional *movie components* pack: Playwright and PyAV for the installed app, on demand.

The installers freeze the engine with PyInstaller but leave out the two video libraries (Playwright,
which drives the system Chrome, and PyAV, which writes the MP4: about 180 MB unpacked, which would
nearly double the app). Instead a zip built by ``scripts/release/build_movie_pack.py`` for each
OS / architecture and the engine's Python is attached to every release, and the installed app
downloads it with the user's consent, keeps it in the user's data folder and loads it at run time:

    <user data dir>/radarx-desktop/movie-pack/<app version>/   (RADARX_MOVIE_PACK_DIR overrides)
        manifest.json          versions, python tag, os, arch, every file with its sha256, licences
        playwright/ av/ ...    the libraries, as ``pip install --target`` lays them out
        licenses/              the licence texts of every component

What this module does and refuses:

* :func:`status` says whether a pack is installed and whether it fits (same app version, same
  Python tag such as ``cpython-312``, same OS and architecture); :func:`verify` re-hashes every
  file.
* :func:`activate` puts the pack directory on ``sys.path`` before ``playwright`` / ``av`` are
  imported (``engine.animate_render.have_module`` calls it). Nothing in the pack is ever executed
  except by importing those libraries; Playwright's own browser downloads are never used (the
  system Chrome is).
* :func:`install` downloads the zip (https, from ``downloads.labshope.com`` unless a test overrides
  the base address), checks the size and the SHA-256 announced by the release index, unpacks into a
  staging folder, refuses zip-slip paths and symlinks, verifies every file against the manifest and
  only then renames the folder into place: the previous pack stays until the new one is verified.
* :func:`remove` deletes the pack folder.

The wheel / ``rxd`` path is unchanged: there the libraries come from ``pip install '...[video]'``.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import stat
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

PRODUCT = "radarx-movie-pack"
SCHEMA = 1
MANIFEST_NAME = "manifest.json"
PACK_HOST = "downloads.labshope.com"
DEFAULT_BASE_URL = f"https://{PACK_HOST}"
INDEX_PATH = "radarx/releases.json"
#: The libraries the pack provides (the names ``have_module`` asks for).
MODULES = ("playwright", "av")
#: Tests and dry runs: another base address (http://127.0.0.1:port) and another folder.
ENV_BASE_URL = "RADARX_MOVIE_PACK_BASE_URL"
ENV_DIR = "RADARX_MOVIE_PACK_DIR"
#: Treat a non-frozen engine like the installed app (the pack is "needed"); for tests and dev runs.
ENV_ENABLED = "RADARX_MOVIE_PACK_ENABLED"
MAX_ZIP_BYTES = 400 * 1024 * 1024
CHUNK = 1 << 20
USER_AGENT = "radarx-desktop-engine (movie components)"

_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class MoviePackError(Exception):
    """The pack cannot be used or installed; the text says why in plain words."""


class MoviePackCancelledError(MoviePackError):
    """The download was cancelled by the user."""


# ---------------------------------------------------------------------------
# Identity: what pack fits this engine
# ---------------------------------------------------------------------------


def app_version() -> str:
    """The version of the app this engine belongs to (what the pack must match)."""
    value = os.environ.get("XRADAR_DESKTOP_VERSION", "").strip()
    if not value:
        try:
            from importlib.metadata import version

            value = version("radarx-desktop-engine")
        except Exception:
            value = ""
    return value if _VERSION_RE.match(value) else "unknown"


def python_tag() -> str:
    """``cpython-312``: compiled extension modules in the pack must be built for exactly this."""
    tag = sys.implementation.cache_tag
    return tag or f"{sys.implementation.name}-{sys.version_info[0]}{sys.version_info[1]}"


def os_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def arch_name() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "aarch64"
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    return m or "unknown"


def pack_name(version: str, os_: str | None = None, arch: str | None = None) -> str:
    return f"{PRODUCT}-{version}-{os_ or os_name()}-{arch or arch_name()}.zip"


def needed() -> bool:
    """True when this engine cannot get the libraries any other way (the installed app)."""
    if os.environ.get(ENV_ENABLED, "").strip() in ("1", "true", "yes"):
        return True
    return bool(getattr(sys, "frozen", False))


# ---------------------------------------------------------------------------
# Where packs live
# ---------------------------------------------------------------------------


def root() -> Path:
    override = os.environ.get(ENV_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    from engine.account.store import config_dir

    return config_dir() / "movie-pack"


def pack_dir(version: str | None = None) -> Path:
    return root() / (version or app_version())


def read_manifest(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MoviePackError("the pack has no manifest") from exc
    except ValueError as exc:
        raise MoviePackError("the pack manifest is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("product") != PRODUCT:
        raise MoviePackError("this is not a movie components pack")
    if data.get("schema") != SCHEMA:
        raise MoviePackError(f"unknown pack format (schema {data.get('schema')!r})")
    for key in ("version", "python", "os", "arch", "files", "components"):
        if key not in data:
            raise MoviePackError(f"the pack manifest lacks {key!r}")
    if not isinstance(data["files"], dict) or not isinstance(data["components"], list):
        raise MoviePackError("the pack manifest is malformed")
    return data


def _fits(manifest: dict[str, Any], version: str) -> str | None:
    """Why this pack does not fit this engine, or None when it does."""
    if manifest["version"] != version:
        return f"it was built for Radarx {manifest['version']}, this is {version}"
    if manifest["python"] != python_tag():
        return f"it was built for Python {manifest['python']}, this engine runs {python_tag()}"
    if manifest["os"] != os_name() or manifest["arch"] != arch_name():
        return (
            f"it was built for {manifest['os']} {manifest['arch']}, "
            f"this computer is {os_name()} {arch_name()}"
        )
    return None


def _folder_size(folder: Path) -> int:
    total = 0
    for p in folder.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return total


def status() -> dict[str, Any]:
    """Whether a usable pack is installed for this app version (no network, no hashing)."""
    version = app_version()
    folder = pack_dir(version)
    out: dict[str, Any] = {
        "needed": needed(),
        "root": str(root()),
        "path": str(folder),
        "app_version": version,
        "python": python_tag(),
        "os": os_name(),
        "arch": arch_name(),
        "installed": False,
        "usable": False,
        "active": folder.resolve() == _ACTIVE if _ACTIVE else False,
        "problem": None,
        "size": 0,
        "pack_version": None,
        "components": [],
        "others": [],
    }
    try:
        if root().is_dir():
            out["others"] = sorted(
                p.name for p in root().iterdir() if p.is_dir() and p.name != version
            )
    except OSError:
        pass
    if not folder.is_dir():
        return out
    out["installed"] = True
    try:
        manifest = read_manifest(folder)
    except MoviePackError as exc:
        out["problem"] = str(exc)
        return out
    out["pack_version"] = manifest["version"]
    out["components"] = [
        {k: c.get(k) for k in ("name", "version", "license")} for c in manifest["components"]
    ]
    out["size"] = int(manifest.get("total_size") or 0) or _folder_size(folder)
    why = _fits(manifest, version)
    if why:
        out["problem"] = f"the installed movie components do not fit: {why}"
        return out
    out["usable"] = True
    return out


def verify(folder: Path | None = None) -> list[str]:
    """Re-hash every file of the installed pack; the list of problems (empty when it is intact)."""
    folder = folder or pack_dir()
    manifest = read_manifest(folder)
    problems: list[str] = []
    for rel, info in sorted(manifest["files"].items()):
        p = folder / Path(*PurePosixPath(rel).parts)
        if not p.is_file():
            problems.append(f"missing: {rel}")
            continue
        if p.stat().st_size != int(info.get("size", -1)) or _sha256(p) != info.get("sha256"):
            problems.append(f"changed: {rel}")
    return problems


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_ACTIVE: Path | None = None
_LOCK = threading.Lock()


def activate() -> bool:
    """Put the installed pack on ``sys.path`` (once); True when a usable pack is active.

    Called before ``playwright`` / ``av`` are looked up. In a development run where the libraries
    are already installed nothing is changed: the environment's copies win.
    """
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None:
            return True
        st = status()
        if not st["usable"]:
            return False
        folder = pack_dir().resolve()
        # Prefer the pack only when the engine has none of the libraries itself (the installed app).
        if not needed() and all(_find_spec(m) for m in MODULES):
            return False
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
        importlib.invalidate_caches()
        _ACTIVE = folder
        logger.info("movie components active: %s", folder)
        return True


def deactivate() -> None:
    """Forget the active pack (after a remove, or in tests)."""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and str(_ACTIVE) in sys.path:
            sys.path.remove(str(_ACTIVE))
        _ACTIVE = None
        for name in list(sys.modules):
            if name.split(".")[0] in MODULES or name.split(".")[0] in ("greenlet", "pyee"):
                sys.modules.pop(name, None)
        importlib.invalidate_caches()


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Finding the pack for this release on the download host
# ---------------------------------------------------------------------------


def base_url(given: str | None = None) -> str:
    """The download host: the test override, else the address the app gave (it must be ours)."""
    override = os.environ.get(ENV_BASE_URL, "").strip()
    if override:
        return override.rstrip("/")
    url = (given or DEFAULT_BASE_URL).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != PACK_HOST or parts.path not in ("", "/"):
        raise MoviePackError(
            f"movie components are only downloaded from https://{PACK_HOST}, not from {url}"
        )
    return f"https://{PACK_HOST}"


def _check_source_url(url: str, base: str) -> None:
    parts = urlsplit(url)
    base_parts = urlsplit(base)
    if parts.scheme != base_parts.scheme or parts.netloc != base_parts.netloc:
        raise MoviePackError(f"refusing to download from {parts.netloc or url!r}: not {base}")
    if parts.scheme != "https" and not os.environ.get(ENV_BASE_URL):
        raise MoviePackError("movie components are downloaded over https only")


def lookup(base: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Ask the download host which pack exists for this app version, Python, OS and architecture.

    One small request (``releases.json``, the same index the website reads). Never raises: the
    answer says ``ok`` with the ``component`` to download, or why there is none (``reason`` is
    ``offline``, ``no-release`` or ``no-pack``) with a sentence for the user.
    """
    version = app_version()
    try:
        url = f"{base_url(base)}/{INDEX_PATH}"
    except MoviePackError as exc:
        return {"ok": False, "reason": "refused", "message": str(exc)}
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
        with urlopen(req, timeout=timeout) as resp:
            index = json.loads(resp.read(4 * 1024 * 1024).decode("utf-8"))
    except Exception as exc:  # DNS, TLS, timeout, 404, bad JSON: all "could not reach it"
        return {
            "ok": False,
            "reason": "offline",
            "message": f"could not read the release index at {PACK_HOST}: {_short(exc)}",
            "url": url,
        }
    releases = index.get("releases") if isinstance(index, dict) else None
    if not isinstance(releases, list):
        return {"ok": False, "reason": "offline", "message": "the release index is malformed"}
    release = next(
        (r for r in releases if isinstance(r, dict) and r.get("version") == version), None
    )
    if release is None:
        return {
            "ok": False,
            "reason": "no-release",
            "message": f"the download host lists no release {version} (this build may be a "
            "candidate that is not published yet, or a development build)",
        }
    want = {"os": os_name(), "arch": arch_name(), "python": python_tag()}
    for comp in release.get("components") or []:
        if not isinstance(comp, dict) or comp.get("kind") != "movie-pack":
            continue
        if all(comp.get(k) == v for k, v in want.items()):
            try:
                _check_component(comp)
            except MoviePackError as exc:
                return {"ok": False, "reason": "refused", "message": str(exc)}
            return {"ok": True, "component": comp, "version": version}
    return {
        "ok": False,
        "reason": "no-pack",
        "message": f"no movie components exist for Radarx {version} on {want['os']} "
        f"{want['arch']} with Python {want['python']}",
    }


def _check_component(comp: dict[str, Any]) -> None:
    name = comp.get("name")
    if not isinstance(name, str) or not _SAFE_PART.match(name) or not name.endswith(".zip"):
        raise MoviePackError("the index names an unexpected pack file")
    if not isinstance(comp.get("size"), int) or comp["size"] <= 0 or comp["size"] > MAX_ZIP_BYTES:
        raise MoviePackError("the index gives no usable size for the pack")
    sha = comp.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise MoviePackError("the index gives no SHA-256 for the pack")
    if not isinstance(comp.get("url"), str) or not comp["url"]:
        raise MoviePackError("the index gives no address for the pack")


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:200]


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

Progress = Callable[[int, str], None]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise MoviePackCancelledError("the download was cancelled")


def download(
    url: str,
    dest: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    base: str | None = None,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    timeout: float = 60.0,
) -> None:
    """Fetch ``url`` to ``dest``; stops as soon as the data exceeds the announced size and refuses
    the file when its SHA-256 is not the announced one (nothing is kept then)."""
    _check_source_url(url, base_url(base))
    if expected_size <= 0 or expected_size > MAX_ZIP_BYTES:
        raise MoviePackError("the announced size of the pack is not plausible")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    got = 0
    t0 = time.monotonic()
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
            length = resp.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) != expected_size:
                raise MoviePackError(
                    f"the host announces {int(length):,} bytes but the release index says "
                    f"{expected_size:,}: not downloading"
                )
            while True:
                _check_cancel(cancel)
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > expected_size:
                    raise MoviePackError("the download is larger than announced: stopped")
                h.update(chunk)
                out.write(chunk)
                if progress:
                    progress(
                        int(90 * got / expected_size),
                        f"downloading {got / 1e6:.0f} of {expected_size / 1e6:.0f} MB",
                    )
        if got != expected_size:
            raise MoviePackError(f"the download ended after {got:,} of {expected_size:,} bytes")
        if h.hexdigest() != expected_sha256:
            raise MoviePackError("the downloaded file does not match its SHA-256: discarded")
        part.replace(dest)
        logger.info(
            "movie components downloaded: %s (%d bytes, %.0f s)", url, got, time.monotonic() - t0
        )
    except MoviePackError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise MoviePackError(f"the download failed: {_short(exc)}") from exc


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath | None:
    """The relative path of a zip member, or None for a directory; refuses anything unsafe."""
    name = info.filename
    if name.endswith("/"):
        return None
    if "\\" in name or name.startswith("/") or ":" in name.split("/", 1)[0]:
        raise MoviePackError(f"refusing a pack with an unsafe path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("..", "", ".") for part in path.parts):
        raise MoviePackError(f"refusing a pack with an unsafe path: {name!r}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise MoviePackError(f"refusing a pack that contains a symbolic link: {name!r}")
    if info.file_size > MAX_ZIP_BYTES:
        raise MoviePackError(f"refusing an implausibly large file in the pack: {name!r}")
    return path


def unpack(zip_path: Path, staging: Path) -> dict[str, Any]:
    """Extract into ``staging`` (which must not exist), verify every file against the manifest,
    and return the manifest. Nothing from the archive is executed."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if zf.testzip() is not None:
                raise MoviePackError("the pack archive is damaged")
            members = [(m, _safe_member(m)) for m in zf.infolist()]
            names = {str(p) for _m, p in members if p is not None}
            if MANIFEST_NAME not in names:
                raise MoviePackError("the pack has no manifest")
            total = sum(m.file_size for m, p in members if p is not None)
            if total > MAX_ZIP_BYTES:
                raise MoviePackError("the pack unpacks to an implausible size")
            for m, rel in members:
                if rel is None:
                    continue
                target = staging.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                mode = (m.external_attr >> 16) & 0o777
                if mode and os.name != "nt":
                    os.chmod(target, mode)
        manifest = read_manifest(staging)
        listed = set(manifest["files"])
        extra = sorted(n for n in names if n != MANIFEST_NAME and n not in listed)
        if extra:
            raise MoviePackError(f"the pack contains files its manifest does not list: {extra[:3]}")
        problems = verify(staging)
        if problems:
            raise MoviePackError("the pack does not match its manifest: " + "; ".join(problems[:3]))
        return manifest
    except zipfile.BadZipFile as exc:
        raise MoviePackError("the pack is not a zip archive") from exc
    except MoviePackError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise MoviePackError(f"could not unpack the movie components: {_short(exc)}") from exc


def install(
    source: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    base: str | None = None,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Download (or take a local zip), verify and put the pack in place; the new :func:`status`.

    ``source`` is the ``url`` of the component from :func:`lookup` (then ``expected_size`` and
    ``expected_sha256`` are required) or the path of a zip already on disk.
    """
    version = app_version()
    if version == "unknown":
        raise MoviePackError("this engine does not know its app version; cannot choose a pack")
    base_dir = root()
    base_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{os.getpid()}-{int(time.time())}"
    staging = base_dir / f".staging-{tag}"
    downloaded = base_dir / f".download-{tag}.zip"
    report = progress or (lambda _p, _m: None)
    try:
        if "://" in source:
            if expected_size is None or not expected_sha256:
                raise MoviePackError("a download needs the size and SHA-256 from the release index")
            report(0, "downloading the movie components")
            download(
                source,
                downloaded,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                base=base,
                progress=report,
                cancel=cancel,
            )
            zip_path = downloaded
        else:
            zip_path = Path(source).expanduser()
            if not zip_path.is_file():
                raise MoviePackError(f"no such file: {zip_path}")
            if expected_sha256 and _sha256(zip_path) != expected_sha256:
                raise MoviePackError("the file does not match the expected SHA-256")
        _check_cancel(cancel)
        report(91, "checking the files")
        manifest = unpack(zip_path, staging)
        why = _fits(manifest, version)
        if why:
            raise MoviePackError(f"these movie components do not fit this app: {why}")
        _check_cancel(cancel)
        report(97, "putting the components in place")
        final = base_dir / version
        old = base_dir / f".old-{tag}"
        with _LOCK:
            if final.exists():
                final.rename(old)
            try:
                staging.rename(final)
            except OSError:
                if old.exists():
                    old.rename(final)
                raise
        shutil.rmtree(old, ignore_errors=True)
        deactivate()
        report(100, "movie components installed")
        return status()
    finally:
        downloaded.unlink(missing_ok=True)
        downloaded.with_name(downloaded.name + ".part").unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def remove() -> dict[str, Any]:
    """Delete every installed pack (all versions) and leftovers; the new :func:`status`."""
    deactivate()
    base_dir = root()
    if base_dir.is_dir():
        for p in base_dir.iterdir():
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink(missing_ok=True)
    return status()


def describe_for_consent(comp: dict[str, Any]) -> dict[str, Any]:
    """What the consent dialog shows: what, from where, how big, and the licences involved."""
    contents = comp.get("contents") or []
    lines = [
        f"{c.get('name')} {c.get('version')} ({c.get('license')})"
        for c in contents
        if isinstance(c, dict)
    ]
    return {
        "name": comp.get("name"),
        "url": comp.get("url"),
        "host": urlsplit(str(comp.get("url", ""))).netloc,
        "size": comp.get("size"),
        "sha256": comp.get("sha256"),
        "contents": lines,
    }
