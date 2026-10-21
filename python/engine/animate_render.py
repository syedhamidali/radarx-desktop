"""Rendering of ``rxd animate``: the app's own 3D renderer in headless Chrome, frames, and the MP4.

The picture is drawn by the same code as the 3D tab (``CartesianRenderer``: the ray-marched volume,
transfer function, lighting, map ground and overlay), loaded in a headless Chrome at
``index.html?clip=1`` and driven frame by frame through ``window.__xrClip`` (see
``src/lib/components/StormClipView.svelte``). Frame *i* is drawn for the clip time *i / fps* in one
full-quality pass, so the result does not depend on how fast the machine is.

Two optional Python packages are used, and only here (never imported by the rest of the engine):

* ``playwright`` drives the system Chrome (``channel="chrome"``; no browser is downloaded);
* ``av`` (PyAV) encodes H.264. Without it the frames are still written as numbered PNG files and
  the command says how to encode them (``ffmpeg``).

Install both with the ``video`` extra: ``pip install 'radarx-desktop-engine[video]'``.
"""

# ruff: noqa: E501
from __future__ import annotations

import contextlib
import functools
import hashlib
import http.server
import importlib.util
import json
import os
import posixpath
import shutil
import socketserver
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.animate_plan import AnimateConfigError
from engine.animate_tiles import TileCache

INSTALL_VIDEO = "pip install 'radarx-desktop-engine[video]'"
INSTALL_VIDEO_CHECKOUT = "pip install -e 'python/[video]'"


def _dependency_error(message: str) -> Exception:
    from engine.api_results import XrdDependencyError

    return XrdDependencyError(message)


# ---------------------------------------------------------------------------
# What is installed
# ---------------------------------------------------------------------------


def have_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def video_status() -> dict[str, bool]:
    """Which of the optional packages ``rxd animate`` uses are importable."""
    return {"playwright": have_module("playwright"), "av": have_module("av")}


def install_help(missing: list[str]) -> str:
    names = {"playwright": "Playwright (drives Chrome)", "av": "PyAV (writes the MP4)"}
    what = " and ".join(names[m] for m in missing)
    return (
        f"rxd animate needs {what}, which are not installed by default. Install the optional "
        f"'video' extra:\n  {INSTALL_VIDEO}\n  (from a source checkout: {INSTALL_VIDEO_CHECKOUT})\n"
        "The system Google Chrome is used: no browser is downloaded. Without PyAV the frames are "
        "still written as PNG files that ffmpeg can encode (the command prints the line)."
    )


def require_playwright() -> None:
    if not have_module("playwright"):
        raise _dependency_error(install_help(["playwright"]))


def require_av() -> None:
    if not have_module("av"):
        raise _dependency_error(install_help(["av"]))


def ffmpeg_command(frames_dir: Path, out: Path, fps: float, ext: str = "png") -> str:
    """The ffmpeg line that turns the numbered frames into an MP4 (printed when PyAV is missing)."""
    return (
        f"ffmpeg -framerate {fps:g} -i '{frames_dir}/frame_%05d.{ext}' "
        f"-c:v libx264 -crf 18 -pix_fmt yuv420p -movflags +faststart '{out}'"
    )


# ---------------------------------------------------------------------------
# The built frontend
# ---------------------------------------------------------------------------


# The installers ship the built frontend as plain files in a ``frontend`` folder next to the
# ``engine`` folder, in the app's resource directory (``bundle.resources`` / ``bundle.macOS.files``):
#   macOS    Radarx.app/Contents/Resources/{engine,frontend}
#   Windows  <install dir>/{engine,frontend}
#   Linux    /usr/lib/Radarx/{engine,frontend}
# and the desktop shell tells the engine where it is (``RADARX_APP_DIST``).
APP_DIST_ENV = ("RADARX_APP_DIST", "XRADAR_APP_DIST")
FRONTEND_RESOURCE = "frontend"


def _frozen_candidates() -> list[Path]:
    """The frontend next to a frozen engine (``<resources>/engine/xradar-engine``)."""
    if not getattr(sys, "frozen", False):
        return []
    out: list[Path] = []
    roots = [Path(sys.executable).resolve().parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass).resolve())
    for root in roots:
        # <resources>/engine/xradar-engine -> <resources>/frontend; _MEIPASS is engine/_internal
        for up in (root, *list(root.parents)[:2]):
            out.append(up / FRONTEND_RESOURCE)
        out.append(root / "dist")
    return out


def frontend_candidates(explicit: str | None = None) -> list[Path]:
    """Places where the built app (``dist/``) is looked for, in order."""
    out: list[Path] = []
    if explicit:
        out.append(Path(explicit).expanduser())
    for name in APP_DIST_ENV:
        env = os.environ.get(name)
        if env:
            out.append(Path(env).expanduser())
    out += _frozen_candidates()
    here = Path(__file__).resolve()
    out += [
        here.parents[2] / "dist",  # a source checkout (pip install -e python/)
        Path.cwd() / "dist",
        here.parent / "_frontend",  # a wheel that ships the frontend (none does yet)
    ]
    # an installed desktop app, for an engine installed with pip (``rxd animate`` outside the app)
    for app in ("Radarx.app", "radarx-desktop.app", "xradar-desktop.app"):
        for apps in (Path("/Applications"), Path.home() / "Applications"):
            out.append(apps / app / "Contents/Resources" / FRONTEND_RESOURCE)
    out += [
        Path("/usr/lib/Radarx") / FRONTEND_RESOURCE,
        Path("/usr/lib/radarx-desktop") / FRONTEND_RESOURCE,
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for name in ("Radarx", "radarx-desktop"):
            out.append(Path(local) / name / FRONTEND_RESOURCE)
    seen: set[Path] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def clip_capable(dist: Path) -> bool:
    """True when the build contains the clip renderer (``window.__xrClip``)."""
    assets = dist / "assets"
    if not assets.is_dir():
        return False
    for js in assets.glob("*.js"):
        try:
            with open(js, "rb") as fh:
                if b"__xrClip" in fh.read():
                    return True
        except OSError:
            continue
    return False


def find_frontend(explicit: str | None = None) -> Path:
    """The built app folder, or a clear error saying how to get one."""
    tried: list[str] = []
    for cand in frontend_candidates(explicit):
        if (cand / "index.html").is_file():
            if clip_capable(cand):
                return cand.resolve()
            tried.append(f"{cand} (found, but it has no clip renderer: rebuild it)")
        else:
            tried.append(str(cand))
    raise _dependency_error(
        "the built app (its frontend) was not found. rxd animate draws with the app's own 3D "
        "renderer, which is in dist/ after `npm run build`:\n"
        "  from a source checkout:   npm ci && npm run build      (rxd then finds <checkout>/dist)\n"
        "  or point to a build:      --app-dist /path/to/dist     or  RADARX_APP_DIST=/path/to/dist\n"
        "  (XRADAR_APP_DIST is also read). An installed app (0.3.0 or later) ships these files in its\n"
        "  'frontend' resource folder, which the app's own engine finds by itself.\n"
        "Looked in: " + "; ".join(tried[:6]) + (" ..." if len(tried) > 6 else "")
    )


def frontend_fingerprint(dist: Path) -> str:
    """Identity of a build for the job key (changes when the app is rebuilt)."""
    idx = dist / "index.html"
    st = idx.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


# ---------------------------------------------------------------------------
# The local server: the app, the bundle and the tiles
# ---------------------------------------------------------------------------


class _Handler(http.server.SimpleHTTPRequestHandler):
    """/ -> the built app, /bundle/ -> manifest + grids, /tiles/ -> cached map tiles."""

    def __init__(
        self,
        *args: Any,
        dist: Path,
        store: Path | None,
        manifest: Path | None,
        tiles: TileCache,
        **kw: Any,
    ) -> None:
        self._dist, self._store, self._manifest, self._tiles = dist, store, manifest, tiles
        super().__init__(*args, **kw)

    def do_GET(self) -> None:
        if self.path.startswith("/tiles/"):
            req = self._tiles.parse(self.path)
            got = self._tiles.get(*req) if req else None
            if got is None:
                self.send_error(404, "tile not available")
                return
            data, ctype = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        clean = posixpath.normpath("/" + path.lstrip("/"))
        if clean.startswith("/bundle/"):
            name = clean[len("/bundle/") :]
            if name == "manifest.json" and self._manifest is not None:
                return str(self._manifest)
            if self._store is not None and "/" not in name:
                return str(self._store / name)
            return str(self._dist / "__missing__")
        return str(self._dist / clean.lstrip("/"))

    def log_message(self, *args: Any) -> None:  # quiet
        pass


def serve(
    dist: Path, store: Path | None, manifest: Path | None, tiles: TileCache
) -> tuple[socketserver.TCPServer, int]:
    """Start the local server on a free port of 127.0.0.1 (nothing is reachable from outside)."""
    handler = functools.partial(_Handler, dist=dist, store=store, manifest=manifest, tiles=tiles)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------------
# Page settings
# ---------------------------------------------------------------------------


@dataclass
class LookArgs:
    preset: str | None = None
    file: str | None = None
    overrides: list[str] | None = None  # "key=value" strings


def parse_overrides(items: list[str] | None) -> dict[str, float | str]:
    """``--set key=value`` pairs; numbers become numbers, everything else stays text."""
    out: dict[str, float | str] = {}
    for item in items or []:
        if "=" not in item or item.startswith("="):
            raise AnimateConfigError(
                f"--set expects key=value (for example --set exposure=1.6), got {item!r}"
            )
        k, v = item.split("=", 1)
        try:
            out[k.strip()] = float(v)
        except ValueError:
            out[k.strip()] = v.strip()
    return out


def look_settings(look: LookArgs) -> dict[str, Any]:
    """The look (docs/REALISTIC_3D.md) as page settings: preset, then file, then ``--set`` on top."""
    s: dict[str, Any] = {}
    if look.preset:
        s["lookPreset"] = look.preset
    if look.file:
        lp = Path(look.file).expanduser()
        if not lp.is_file():
            raise AnimateConfigError(f"--look: file not found: {lp}")
        s["lookText"] = lp.read_text(encoding="utf-8")
        s["lookFormat"] = "toml" if lp.suffix.lower() == ".toml" else "json"
    ov = parse_overrides(look.overrides)
    if ov:
        s["lookOverrides"] = ov
    return s


def chrome_args(software: bool = False) -> list[str]:
    """Chrome switches: the GPU on, no proxy (only the local server and the tile proxy are used).

    ``software``: no GPU, Chrome's software renderer (SwiftShader): works anywhere, very slow."""
    args = ["--no-proxy-server", "--ignore-gpu-blocklist", "--enable-gpu-rasterization"]
    if software:
        args += ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    elif sys.platform == "darwin":
        args += ["--use-gl=angle", "--use-angle=metal"]
    else:
        args += ["--use-gl=angle", "--enable-unsafe-swiftshader"]
    extra = os.environ.get("XRADAR_CHROME_ARGS", "").split()
    return args + extra


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------


class Renderer:
    """A headless Chrome showing the clip page; draws frames on demand."""

    def __init__(
        self,
        dist: Path,
        width: int,
        height: int,
        *,
        store: Path | None = None,
        manifest: Path | None = None,
        tile_cache: str | None = None,
        offline: bool = False,
        channel: str = "chrome",
        software: bool = False,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        require_playwright()
        self.log = log
        self.width, self.height = width, height
        self.tiles = TileCache(tile_cache, offline=offline)
        self.srv, self.port = serve(dist, store, manifest, self.tiles)
        self._pw: Any = None
        self._browser: Any = None
        self.page: Any = None
        self.software = software
        self.messages: list[str] = []
        try:
            self._start(channel, software)
        except BaseException:
            self.close()
            raise

    def _start(self, channel: str, software: bool = False) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                channel=channel or None, headless=True, args=chrome_args(software)
            )
        except Exception as exc:
            text = str(exc)
            if (
                "Executable doesn't exist" in text
                or "Failed to launch" in text
                or "not found" in text
            ):
                raise _dependency_error(
                    f"could not start the browser (channel '{channel}'). rxd animate uses the system "
                    "Google Chrome: install Chrome (https://www.google.com/chrome/), or use another "
                    "Chromium with --browser chromium after `python -m playwright install chromium`. "
                    f"Details: {text.splitlines()[0][:160]}"
                ) from exc
            raise
        self.page = self._browser.new_page(
            viewport={"width": self.width, "height": self.height}, device_scale_factor=1
        )
        self.page.on(
            "console",
            lambda m: (
                self.messages.append(f"{m.type}: {m.text[:300]}")
                if m.type in ("error", "warning")
                else None
            ),
        )
        self.page.on("pageerror", lambda e: self.messages.append(f"PAGEERROR: {str(e)[:300]}"))
        self.page.goto(f"http://127.0.0.1:{self.port}/?clip=1")
        self.page.wait_for_function("() => !!window.__xrClip", timeout=30000)

    @property
    def browser_version(self) -> str | None:
        try:
            return str(self._browser.version)
        except Exception:
            return None

    def _call(self, expr: str, arg: Any = None) -> Any:
        try:
            return self.page.evaluate(expr, arg) if arg is not None else self.page.evaluate(expr)
        except Exception as exc:
            msg = str(exc).split("\n")[0].replace("Page.evaluate: ", "")
            msg = msg.removeprefix("Error: ")
            raise AnimateConfigError(msg) from exc

    def check_look(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Resolve the look of ``settings`` (preset, file, ``--set``) and return it; a bad key raises
        an error naming the key and the nearest known one, before any gridding is done."""
        return self._call("(s) => window.__xrClip.checkLook(s)", settings)

    def load(self, settings: dict[str, Any]) -> dict[str, Any]:
        info = self._call("(a) => window.__xrClip.load(a[0], a[1])", ["/bundle", settings])
        self._call("(p) => window.__xrClip.prepare(p)", settings.get("basemap", "osm"))
        st = self._call("() => window.__xrClip.info()")
        if st.get("fatal"):
            raise RuntimeError(f"renderer error: {st['fatal']}")
        info["state"] = st
        return info

    def frame_count(self) -> int:
        return int(self._call("() => window.__xrClip.frameCount()"))

    def report(self) -> dict[str, Any]:
        return self._call("() => window.__xrClip.report()")

    def render_audio(self, sample_rate: int = 48000) -> dict[str, Any]:
        """Synthesise the soundscape in the page (offline Web Audio): 16-bit stereo PCM (base64) and a
        report of what it was made from. Never returns an empty result: a failure is an error."""
        try:
            return self.page.evaluate("(sr) => window.__xrClip.renderAudio(sr)", sample_rate)
        except Exception as exc:
            msg = str(exc).split("\n")[0].replace("Page.evaluate: ", "").removeprefix("Error: ")
            if "OfflineAudioContext" in msg or "AudioContext" in msg:
                raise _dependency_error(
                    "this Chrome cannot synthesise the sound (no offline Web Audio): update Google Chrome "
                    f"(details: {msg[:160]})"
                ) from exc
            raise RuntimeError(f"could not synthesise the sound: {msg[:300]}") from exc

    def frame_png(self, i: int) -> bytes:
        self._call("(i) => window.__xrClip.renderFrame(i)", i)
        return self.page.screenshot(type="png")

    def frame_at(self, t: float) -> bytes:
        self._call("(t) => window.__xrClip.renderAt(t)", t)
        return self.page.screenshot(type="png")

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                with contextlib.suppress(Exception):
                    getattr(obj, meth)()
        self._browser = self._pw = None
        if getattr(self, "srv", None) is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def __enter__(self) -> Renderer:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Frames on disk, resume and encoding
# ---------------------------------------------------------------------------


def frame_name(i: int, ext: str = "png") -> str:
    return f"frame_{i:05d}.{ext}"


def job_key(parts: dict[str, Any]) -> str:
    """Identity of a render job: the same inputs, settings and size give the same frames."""
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:16]


def existing_frames(frames_dir: Path, n: int, ext: str = "png") -> set[int]:
    """Indices of complete frames already in ``frames_dir`` (a frame is written atomically)."""
    if not frames_dir.is_dir():
        return set()
    done = set()
    for i in range(n):
        p = frames_dir / frame_name(i, ext)
        try:
            if p.stat().st_size > 0:
                done.add(i)
        except OSError:
            pass
    return done


def save_frame(png: bytes, path: Path, ext: str = "png", quality: int = 95) -> None:
    """Write a frame atomically (PNG as given; JPEG re-encoded from the screenshot)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    if ext == "png":
        tmp.write_bytes(png)
    else:
        import io

        from PIL import Image

        Image.open(io.BytesIO(png)).convert("RGB").save(tmp, "JPEG", quality=quality, subsampling=0)
    tmp.replace(path)


def render_frames(
    r: Renderer,
    frames_dir: Path,
    indices: list[int],
    total: int,
    *,
    ext: str = "png",
    progress: Callable[[int, int, float], None] | None = None,
) -> None:
    """Draw and save the frames ``indices`` (each is independent of the others)."""
    t0 = time.perf_counter()
    for n, i in enumerate(indices):
        save_frame(r.frame_png(i), frames_dir / frame_name(i, ext), ext)
        if progress:
            progress(n + 1, len(indices), time.perf_counter() - t0)


@dataclass
class AudioTrack:
    """Audio to put in the MP4: 16-bit stereo PCM, shape (samples, 2), at ``sample_rate`` Hz."""

    pcm: Any
    sample_rate: int = 48000


def pcm_from_page(audio: dict[str, Any], n_frames: int, fps: float) -> AudioTrack:
    """The PCM of ``renderAudio`` cut or padded with silence to exactly the length of the video."""
    import base64

    import numpy as np

    sr = int(audio["sampleRate"])
    pcm = np.frombuffer(base64.b64decode(audio["pcm"]), dtype="<i2").reshape(-1, 2)
    want = round(n_frames / fps * sr)
    if len(pcm) > want:
        pcm = pcm[:want]
    elif len(pcm) < want:
        pcm = np.vstack([pcm, np.zeros((want - len(pcm), 2), dtype="<i2")])
    return AudioTrack(np.ascontiguousarray(pcm), sr)


def encode_mp4(
    frames_dir: Path,
    n: int,
    out: Path,
    fps: float,
    size: tuple[int, int],
    *,
    crf: int = 18,
    preset: str = "medium",
    ext: str = "png",
    metadata: dict[str, str] | None = None,
    audio: AudioTrack | None = None,
) -> None:
    """H.264 (yuv420p, faststart) from the numbered frames; written to ``out.part`` then renamed, so
    a crash never leaves a broken file under the final name. ``audio``: an AAC 48 kHz stereo track of
    the same length, interleaved with the video."""
    require_av()
    import av
    from PIL import Image

    part = out.with_name(out.name + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(part), "w", format="mp4", options={"movflags": "+faststart"})
    try:
        for k, v in (metadata or {}).items():
            container.metadata[k] = v
        stream = container.add_stream("libx264", rate=round(fps))
        w, h = size[0] - size[0] % 2, size[1] - size[1] % 2
        stream.width, stream.height = w, h
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(crf), "preset": preset}
        audio_packets = _encode_audio(container, audio) if audio is not None else []
        for i in range(n):
            img = Image.open(frames_dir / frame_name(i, ext)).convert("RGB")
            if img.size != (w, h):
                img = img.crop((0, 0, w, h))
            for pkt in stream.encode(av.VideoFrame.from_image(img)):
                container.mux(pkt)
            # the audio up to this frame's time goes in with it (a well interleaved file)
            while audio_packets and audio_packets[0].pts * audio_packets[0].time_base <= (
                i + 1
            ) / round(fps):
                container.mux(audio_packets.pop(0))
        for pkt in stream.encode():
            container.mux(pkt)
        for pkt in audio_packets:
            container.mux(pkt)
    except BaseException:
        container.close()
        part.unlink(missing_ok=True)
        raise
    container.close()
    part.replace(out)


def _encode_audio(container: Any, audio: AudioTrack) -> list[Any]:
    """Add an AAC stereo stream to ``container`` and return its packets (muxed by the caller)."""
    from fractions import Fraction

    import av
    import numpy as np

    sr = int(audio.sample_rate)
    stream = container.add_stream("aac", rate=sr)
    stream.layout = "stereo"
    stream.bit_rate = 160_000
    planar = np.ascontiguousarray((audio.pcm.astype(np.float32) / 32768.0).T)
    packets: list[Any] = []
    for i0 in range(0, planar.shape[1], 1024):
        af = av.AudioFrame.from_ndarray(planar[:, i0 : i0 + 1024], format="fltp", layout="stereo")
        af.sample_rate = sr
        af.pts = i0
        af.time_base = Fraction(1, sr)
        packets.extend(stream.encode(af))
    packets.extend(stream.encode())
    return packets


def copy_frames(frames_dir: Path, dest: Path, n: int, ext: str = "png") -> None:
    """The PNG sequence as the result (``-o some_folder/``)."""
    dest.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        shutil.copyfile(frames_dir / frame_name(i, ext), dest / frame_name(i, ext))
