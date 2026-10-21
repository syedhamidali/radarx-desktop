#!/usr/bin/env python3
"""Build the optional *movie components* pack for this OS, architecture and Python.

The installers leave Playwright (drives the system Chrome) and PyAV (writes the MP4) out of the
frozen engine; this script makes the zip the installed app downloads on demand
(``python/engine/movie_pack.py`` loads it):

    radarx-movie-pack-<version>-<os>-<arch>.zip            the pack
    radarx-movie-pack-<version>-<os>-<arch>.manifest.json  its manifest without the file list
                                                           (the release index reads it)

It runs ``pip install --target`` with EXACT pinned versions of playwright, av and what they need
(greenlet, pyee, typing_extensions), with the SAME interpreter as the frozen engine (the Python tag,
e.g. ``cpython-312``, is written into the manifest and checked by the engine, because greenlet is a
compiled extension built for one Python), strips what is never loaded (tests, type stubs, Cython
headers, bytecode caches, Playwright's trace viewer and recorder UIs), never downloads Playwright's
own browsers (the pack relies on the system Chrome), copies every component's licence files into
``licenses/`` and writes ``manifest.json`` with the SHA-256 of every file.

Usage:  python scripts/release/build_movie_pack.py --out dist/movie-pack [--version 0.3.1]
        (run it with the engine's Python: the one that built python/dist/xradar-engine)
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PRODUCT = "radarx-movie-pack"
SCHEMA = 1
MANIFEST_NAME = "manifest.json"

#: Exact versions (the same for every platform and release of the app that uses them). Bump all
#: together and rebuild the packs for a release. These are the latest versions of 2026-10 that
#: python/pyproject.toml's ``video`` extra accepts.
PINS: dict[str, str] = {
    "playwright": "1.64.0",
    "av": "19.0.2",
    "greenlet": "3.5.6",
    "pyee": "14.0.0",
    "typing_extensions": "4.15.0",
}

#: name -> (distribution name on PyPI, what it does, licence as declared upstream)
COMPONENTS: dict[str, tuple[str, str, str]] = {
    "playwright": (
        "playwright",
        "drives the system Google Chrome that draws the frames",
        "Apache-2.0",
    ),
    "av": (
        "av",
        "PyAV: writes the MP4 (H.264 video, AAC sound) with the FFmpeg libraries in its wheel",
        "BSD-3-Clause (PyAV); the bundled FFmpeg libraries are LGPL-2.1-or-later with GPL-2.0-or-later parts (libx264, libx265)",
    ),
    "greenlet": ("greenlet", "needed by Playwright's synchronous API", "MIT AND PSF-2.0"),
    "pyee": ("pyee", "needed by Playwright (events)", "MIT"),
    "typing_extensions": ("typing_extensions", "needed by pyee", "PSF-2.0"),
}

#: Files that are never loaded at run time (relative POSIX path inside the pack, regex).
DROP_PATTERNS = [
    r"(^|/)__pycache__(/|$)",
    r"\.py[co]$",
    r"(^|/)(tests?|testing)(/|$)",
    r"\.(pxd|pxi|pyi|h|hpp|c|cc|cpp)$",
    r"^av/include/",
    r"^playwright/driver/package/lib/vite/",  # trace viewer, recorder, HTML report UIs
    r"^playwright/driver/package/types/",
    r"^playwright/driver/package/index\.d\.ts$",
    r"^playwright/driver/package/README\.md$",
    r"^playwright/driver/package/bin/.*\.ps1$",
    r"^[^/]+\.dist-info/RECORD$",  # paths and hashes of the original layout; the manifest replaces it
    r"^[^/]+\.dist-info/(REQUESTED|INSTALLER|direct_url\.json)$",
]
_DROP = [re.compile(p) for p in DROP_PATTERNS]

#: Licence and notice files of the Node driver inside the playwright wheel (not in its dist-info).
PLAYWRIGHT_DRIVER_NOTICES = ("LICENSE", "NOTICE", "ThirdPartyNotices.txt")

#: What the bundled FFmpeg is: written into the manifest and the pack's ``licenses/av/`` folder.
FFMPEG_NOTE = (
    "The av wheel bundles shared libraries of FFmpeg (libavcodec, libavformat, libavutil, libavfilter, "
    "libavdevice, libswscale, libswresample) and codecs it is built with (libx264, libx265, libvpx, "
    "libopus, dav1d, SVT-AV1, libwebp, libvmaf). FFmpeg is licensed under the LGPL-2.1-or-later; built "
    "with libx264 and libx265 (GPL-2.0-or-later) the combination is distributed under the GPL-2.0-or-later. "
    "Radarx uses libx264 to write H.264. These libraries are unmodified, separate files loaded at run "
    "time; their source is at https://ffmpeg.org/ and https://github.com/PyAV-Org/pyav-ffmpeg."
)


class PackBuildError(Exception):
    """The pack cannot be built as asked; the text says why."""


# --- identity ------------------------------------------------------------------------------------


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
    raise PackBuildError(f"unsupported architecture {m!r}")


def python_tag() -> str:
    return sys.implementation.cache_tag


def default_version() -> str:
    """The app version from python/pyproject.toml (the pack must match the app that uses it)."""
    root = Path(__file__).resolve().parents[2]
    text = (root / "python" / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    if not m:
        raise PackBuildError("no version in python/pyproject.toml")
    return m.group(1)


def pack_base_name(version: str) -> str:
    return f"{PRODUCT}-{version}-{os_name()}-{arch_name()}"


# --- the libraries --------------------------------------------------------------------------------


def pip_install(target: Path, python: str = sys.executable) -> None:
    """``pip install --target`` of the pinned wheels only (no dependency resolution, no sdists)."""
    env = dict(os.environ)
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"  # belt and braces: pip never downloads browsers
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    cmd = [
        python,
        "-m",
        "pip",
        "install",
        "--target",
        str(target),
        "--no-deps",
        "--no-compile",
        "--only-binary=:all:",
        "--upgrade",
        *[f"{name}=={ver}" for name, ver in PINS.items()],
    ]
    print("+", " ".join(cmd[2:]))
    subprocess.run(cmd, check=True, env=env)


def dropped(rel: str) -> bool:
    return any(r.search(rel) for r in _DROP)


def strip_tree(target: Path) -> tuple[int, int]:
    """Delete what is never loaded; returns (files removed, bytes removed)."""
    removed, size = 0, 0
    for p in sorted(target.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        rel = p.relative_to(target).as_posix()
        if p.is_symlink():
            raise PackBuildError(f"symbolic link in the installed libraries: {rel}")
        if p.is_file() and dropped(rel):
            size += p.stat().st_size
            p.unlink()
            removed += 1
        elif p.is_dir() and (dropped(rel + "/") or not any(p.iterdir())):
            shutil.rmtree(p)
    for name in ("bin", "Scripts"):  # console scripts pip writes next to --target
        shutil.rmtree(target / name, ignore_errors=True)
    if (target / "ms-playwright").exists() or any(target.rglob("chrome-linux")):
        raise PackBuildError("a browser was downloaded into the pack; it must never be")
    return removed, size


def dist_info(target: Path, dist: str) -> Path:
    cands = sorted(target.glob(f"{dist.replace('-', '_')}-*.dist-info"))
    if not cands:
        raise PackBuildError(f"{dist}: no dist-info after pip install")
    return cands[0]


def installed_version(info: Path) -> str:
    for line in (info / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    raise PackBuildError(f"{info.name}: no Version in METADATA")


def collect_licenses(target: Path) -> list[dict]:
    """Copy every component's licence texts into ``licenses/<name>/`` and describe the components."""
    out = []
    lic_root = target / "licenses"
    for name, (dist, role, license_text) in COMPONENTS.items():
        info = dist_info(target, dist)
        version = installed_version(info)
        if version != PINS[name]:
            raise PackBuildError(f"{name}: pip installed {version}, not the pinned {PINS[name]}")
        dest = lic_root / name
        dest.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        sources = sorted((info / "licenses").rglob("*")) if (info / "licenses").is_dir() else []
        sources += [
            p for p in info.iterdir() if p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
        ]
        for src in sources:
            if src.is_file() and not dropped(src.relative_to(target).as_posix()):
                tgt = dest / src.name
                shutil.copyfile(src, tgt)
                files.append(tgt.relative_to(target).as_posix())
        if name == "playwright":
            for n in PLAYWRIGHT_DRIVER_NOTICES:
                src = target / "playwright" / "driver" / "package" / n
                if src.is_file():
                    tgt = dest / f"driver-{n}"
                    shutil.copyfile(src, tgt)
                    files.append(tgt.relative_to(target).as_posix())
        if name == "av":
            (dest / "FFMPEG-NOTICE.txt").write_text(FFMPEG_NOTE + "\n", encoding="utf-8")
            files.append((dest / "FFMPEG-NOTICE.txt").relative_to(target).as_posix())
        if not files:
            raise PackBuildError(f"{name}: no licence file found to ship")
        entry = {
            "name": name,
            "distribution": dist,
            "version": version,
            "role": role,
            "license": license_text,
            "license_files": sorted(files),
        }
        if name == "av":
            # the shared libraries of FFmpeg and its codecs (av/.dylibs on macOS, av.libs on
            # Linux, next to the modules on Windows), not PyAV's own extension modules
            entry["bundled_libraries"] = sorted(
                p.name
                for p in target.rglob("*")
                if p.is_file()
                and p.relative_to(target).parts[0] in ("av", "av.libs")
                and p.name.startswith(("lib", "av", "sw"))
                and (".dylib" in p.name or ".so" in p.name or p.suffix == ".dll")
                and ".abi3." not in p.name
                and "cpython" not in p.name
            )
            entry["note"] = FFMPEG_NOTE
        out.append(entry)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def component_sizes(target: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for p in target.rglob("*"):
        if p.is_file():
            top = p.relative_to(target).parts[0]
            key = (
                top.split("-")[0].split(".")[0]
                if top.endswith(".dist-info")
                else top.removesuffix(".py")
            )
            sizes[key] = sizes.get(key, 0) + p.stat().st_size
    return sizes


def write_manifest(target: Path, version: str, components: list[dict]) -> dict:
    files = {}
    for p in sorted(target.rglob("*")):
        if p.is_file() and p.name != MANIFEST_NAME:
            rel = p.relative_to(target).as_posix()
            files[rel] = {"size": p.stat().st_size, "sha256": _sha256(p)}
    manifest = {
        "schema": SCHEMA,
        "product": PRODUCT,
        "version": version,
        "os": os_name(),
        "arch": arch_name(),
        "python": python_tag(),
        "python_version": platform.python_version(),
        "platform": sysconfig.get_platform(),
        "components": components,
        "total_size": sum(f["size"] for f in files.values()),
        "file_count": len(files),
        "files": files,
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def write_zip(target: Path, out_zip: Path) -> None:
    """A deterministic zip (sorted, fixed time stamps, executable bits kept for the Node driver)."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.with_name(out_zip.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(target).as_posix()
            if PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
                raise PackBuildError(f"unsafe path {rel!r}")
            info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = p.stat().st_mode & 0o777
            info.external_attr = (0o100000 | mode) << 16
            with open(p, "rb") as fh:
                zf.writestr(info, fh.read())
    tmp.replace(out_zip)


def build(out_dir: Path, version: str, *, python: str = sys.executable, keep: bool = False) -> Path:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise PackBuildError(f"{version!r} is not a version like 0.3.1")
    if python != sys.executable:
        tag = subprocess.run(
            [python, "-c", "import sys; print(sys.implementation.cache_tag)"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if tag != python_tag():
            raise PackBuildError(
                f"run this script with the engine's Python: {python} is {tag}, this is {python_tag()}"
            )
    work = Path(tempfile.mkdtemp(prefix="movie-pack-"))
    target = work / "pack"
    try:
        pip_install(target, python)
        removed, bytes_removed = strip_tree(target)
        print(f"stripped {removed} files ({bytes_removed / 1e6:.1f} MB) that are never loaded")
        components = collect_licenses(target)
        manifest = write_manifest(target, version, components)
        base = pack_base_name(version)
        out_zip = out_dir / f"{base}.zip"
        write_zip(target, out_zip)
        side = {k: v for k, v in manifest.items() if k != "files"}
        side["zip"] = {
            "name": out_zip.name,
            "size": out_zip.stat().st_size,
            "sha256": _sha256(out_zip),
        }
        (out_dir / f"{base}.manifest.json").write_text(
            json.dumps(side, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        sizes = component_sizes(target)
        print(
            f"\n{out_zip.name}: {out_zip.stat().st_size / 1e6:.1f} MB compressed, "
            f"{manifest['total_size'] / 1e6:.1f} MB unpacked ({manifest['file_count']} files), "
            f"{manifest['os']} {manifest['arch']} {manifest['python']}"
        )
        for name in sorted(sizes, key=lambda k: -sizes[k]):
            print(f"  {name:20s} {sizes[name] / 1e6:8.1f} MB")
        if keep:
            kept = out_dir / f"{base}-unpacked"
            shutil.rmtree(kept, ignore_errors=True)
            shutil.copytree(target, kept)
            print("unpacked copy:", kept)
        return out_zip
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("dist/movie-pack"), help="output folder")
    ap.add_argument("--version", help="app version (default: python/pyproject.toml)")
    ap.add_argument(
        "--python", default=sys.executable, help="interpreter whose pip installs the wheels"
    )
    ap.add_argument(
        "--keep", action="store_true", help="also leave the unpacked folder next to the zip"
    )
    args = ap.parse_args(argv)
    try:
        build(args.out, args.version or default_version(), python=args.python, keep=args.keep)
    except (PackBuildError, subprocess.CalledProcessError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
