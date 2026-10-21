"""The optional movie components pack: status, verification, install (download + unpack), activate,
remove, and what is refused (zip-slip, wrong Python tag, wrong checksum, another host)."""

from __future__ import annotations

import hashlib
import http.server
import json
import stat
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from engine import animate_render, movie_pack

VERSION = "9.9.9"


@pytest.fixture
def pack_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XRADAR_DESKTOP_VERSION", VERSION)
    monkeypatch.setenv(movie_pack.ENV_DIR, str(tmp_path / "packs"))
    monkeypatch.setenv(movie_pack.ENV_ENABLED, "1")
    movie_pack.deactivate()
    yield tmp_path
    movie_pack.deactivate()


def make_pack(
    out: Path,
    *,
    version: str = VERSION,
    python: str | None = None,
    files: dict[str, bytes] | None = None,
    extra_unlisted: dict[str, bytes] | None = None,
    bad_hash: str | None = None,
    manifest_patch: dict | None = None,
) -> Path:
    """A small pack with pure-Python stand-ins for the libraries (``playwright`` and ``av``)."""
    files = files or {
        "playwright/__init__.py": b"PACK = 'playwright from the pack'\n",
        "playwright/sync_api.py": b"def sync_playwright():\n    return 'fake'\n",
        "av/__init__.py": b"PACK = 'av from the pack'\n",
        "licenses/playwright/LICENSE": b"Apache-2.0 text\n",
    }
    manifest = {
        "schema": movie_pack.SCHEMA,
        "product": movie_pack.PRODUCT,
        "version": version,
        "python": python or movie_pack.python_tag(),
        "os": movie_pack.os_name(),
        "arch": movie_pack.arch_name(),
        "components": [
            {"name": "playwright", "version": "1.0", "license": "Apache-2.0"},
            {"name": "av", "version": "2.0", "license": "BSD-3-Clause"},
        ],
        "total_size": sum(len(b) for b in files.values()),
        "files": {
            n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()
        },
    }
    if bad_hash:
        manifest["files"][bad_hash]["sha256"] = "0" * 64
    if manifest_patch:
        manifest.update(manifest_patch)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if name.endswith(".py") else 0o100644) << 16
            zf.writestr(info, data)
        for name, data in (extra_unlisted or {}).items():
            zf.writestr(name, data)
        zf.writestr(movie_pack.MANIFEST_NAME, json.dumps(manifest))
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- identity and status -------------------------------------------------------------------------


def test_identity_and_names(pack_env):
    assert movie_pack.app_version() == VERSION
    assert movie_pack.python_tag().startswith(("cpython-", "pypy-"))
    assert movie_pack.os_name() in ("macos", "windows", "linux")
    assert movie_pack.arch_name() in ("aarch64", "x86_64")
    assert movie_pack.pack_name(VERSION) == (
        f"radarx-movie-pack-{VERSION}-{movie_pack.os_name()}-{movie_pack.arch_name()}.zip"
    )
    assert movie_pack.needed()  # ENV_ENABLED stands in for sys.frozen


def test_status_without_a_pack(pack_env):
    st = movie_pack.status()
    assert st["installed"] is False and st["usable"] is False and st["problem"] is None
    assert st["app_version"] == VERSION and st["path"].endswith(VERSION)
    assert not movie_pack.activate()


def test_base_url_only_the_download_host(pack_env, monkeypatch):
    assert movie_pack.base_url(None) == "https://downloads.labshope.com"
    assert (
        movie_pack.base_url("https://downloads.labshope.com/") == "https://downloads.labshope.com"
    )
    for bad in (
        "http://downloads.labshope.com",
        "https://evil.example.com",
        "https://downloads.labshope.com/x",
    ):
        with pytest.raises(movie_pack.MoviePackError):
            movie_pack.base_url(bad)
    monkeypatch.setenv(movie_pack.ENV_BASE_URL, "http://127.0.0.1:1/")
    assert movie_pack.base_url("https://evil.example.com") == "http://127.0.0.1:1"


# --- install from a local file ---------------------------------------------------------------------


def test_install_from_a_local_zip_then_activate_import_and_remove(pack_env):
    zip_path = make_pack(pack_env / "pack.zip")
    before = set(sys.path)
    st = movie_pack.install(str(zip_path))
    assert st["installed"] and st["usable"] and st["problem"] is None
    assert [c["name"] for c in st["components"]] == ["playwright", "av"]
    folder = Path(st["path"])
    assert (folder / "playwright" / "sync_api.py").is_file()
    if sys.platform != "win32":
        assert stat.S_IMODE((folder / "playwright" / "sync_api.py").stat().st_mode) & 0o100
    assert movie_pack.verify(folder) == []

    # the loader: have_module() activates the pack before looking the libraries up
    assert animate_render.have_module("playwright") and animate_render.have_module("av")
    assert str(folder) in sys.path and str(folder) not in before
    import av

    assert av.PACK == "av from the pack"
    assert movie_pack.status()["active"] is True

    st = movie_pack.remove()
    assert not st["installed"] and not folder.exists()
    assert str(folder) not in sys.path


def test_install_keeps_the_old_pack_when_the_new_one_is_bad(pack_env):
    movie_pack.install(str(make_pack(pack_env / "good.zip")))
    folder = movie_pack.pack_dir()
    marker = sha(folder / "av" / "__init__.py")
    bad = make_pack(pack_env / "bad.zip", bad_hash="av/__init__.py")
    with pytest.raises(movie_pack.MoviePackError, match="does not match its manifest"):
        movie_pack.install(str(bad))
    assert sha(folder / "av" / "__init__.py") == marker
    assert movie_pack.status()["usable"]
    assert not [p for p in movie_pack.root().iterdir() if p.name.startswith(".")]  # no leftovers


def test_install_replaces_an_older_pack_atomically(pack_env):
    movie_pack.install(str(make_pack(pack_env / "a.zip")))
    files = {
        "playwright/__init__.py": b"PACK = 'second'\n",
        "av/__init__.py": b"PACK = 'second'\n",
    }
    movie_pack.install(str(make_pack(pack_env / "b.zip", files=files)))
    folder = movie_pack.pack_dir()
    assert (folder / "av" / "__init__.py").read_bytes() == b"PACK = 'second'\n"
    assert not (folder / "playwright" / "sync_api.py").exists()
    assert sorted(p.name for p in movie_pack.root().iterdir()) == [VERSION]


@pytest.mark.parametrize(
    "how, text",
    [
        ("python", "built for Python"),
        ("version", "built for Radarx"),
        ("os", "built for"),
    ],
)
def test_a_pack_for_another_engine_is_refused(pack_env, how, text):
    kw = {}
    if how == "python":
        kw["python"] = "cpython-27"
    elif how == "version":
        kw["version"] = "0.0.1"
    else:
        kw["manifest_patch"] = {"os": "plan9"}
    with pytest.raises(movie_pack.MoviePackError, match=text):
        movie_pack.install(str(make_pack(pack_env / "p.zip", **kw)))
    assert not movie_pack.status()["installed"]


def test_zip_slip_symlinks_and_unlisted_files_are_refused(pack_env):
    for name in ("../escape.py", "/abs.py", "a/../../b.py"):
        z = pack_env / "slip.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(movie_pack.MANIFEST_NAME, "{}")
            zf.writestr(name, b"x")
        with pytest.raises(movie_pack.MoviePackError, match="unsafe path"):
            movie_pack.install(str(z))
    z = pack_env / "link.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("av/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, b"/etc/passwd")
        zf.writestr(movie_pack.MANIFEST_NAME, "{}")
    with pytest.raises(movie_pack.MoviePackError, match="symbolic link"):
        movie_pack.install(str(z))
    z = make_pack(pack_env / "extra.zip", extra_unlisted={"av/sneaky.py": b"print(1)\n"})
    with pytest.raises(movie_pack.MoviePackError, match="does not list"):
        movie_pack.install(str(z))
    (pack_env / "notzip.zip").write_bytes(b"hello")
    with pytest.raises(movie_pack.MoviePackError, match="not a zip"):
        movie_pack.install(str(pack_env / "notzip.zip"))
    assert not movie_pack.status()["installed"]


def test_verify_notices_a_changed_file(pack_env):
    movie_pack.install(str(make_pack(pack_env / "p.zip")))
    folder = movie_pack.pack_dir()
    (folder / "av" / "__init__.py").write_bytes(b"tampered")
    assert movie_pack.verify(folder) == ["changed: av/__init__.py"]
    (folder / "av" / "__init__.py").unlink()
    assert movie_pack.verify(folder) == ["missing: av/__init__.py"]


# --- download from a host --------------------------------------------------------------------------


class _Server:
    """A local http server standing in for the download host (``ENV_BASE_URL`` points at it)."""

    def __init__(self, folder: Path):
        handler = type(
            "H",
            (http.server.SimpleHTTPRequestHandler,),
            {
                "__init__": lambda s, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(
                    s, *a, directory=str(folder), **k
                ),
                "log_message": lambda *a: None,
            },
        )
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def host(pack_env, monkeypatch):
    folder = pack_env / "host" / "radarx" / f"v{VERSION}"
    folder.mkdir(parents=True)
    srv = _Server(pack_env / "host")
    monkeypatch.setenv(movie_pack.ENV_BASE_URL, srv.url)
    yield srv, folder
    srv.close()


def _index(base: str, name: str, size: int, digest: str, *, python: str | None = None) -> dict:
    return {
        "schema": 1,
        "product": "radarx",
        "latest": VERSION,
        "releases": [
            {
                "version": VERSION,
                "date": "2026-10-21T00:00:00Z",
                "notes": "",
                "assets": [],
                "components": [
                    {
                        "kind": "movie-pack",
                        "name": name,
                        "os": movie_pack.os_name(),
                        "arch": movie_pack.arch_name(),
                        "python": python or movie_pack.python_tag(),
                        "size": size,
                        "sha256": digest,
                        "url": f"{base}/radarx/v{VERSION}/{name}",
                        "contents": [
                            {"name": "playwright", "version": "1.0", "license": "Apache-2.0"}
                        ],
                    }
                ],
            }
        ],
    }


def test_lookup_download_install_with_progress(host):
    srv, folder = host
    name = movie_pack.pack_name(VERSION)
    zip_path = make_pack(folder / name)
    (folder.parent / "releases.json").write_text(
        json.dumps(_index(srv.url, name, zip_path.stat().st_size, sha(zip_path)))
    )
    found = movie_pack.lookup()
    assert found["ok"], found
    comp = found["component"]
    consent = movie_pack.describe_for_consent(comp)
    assert consent["host"].startswith("127.0.0.1") and consent["size"] == zip_path.stat().st_size
    assert consent["contents"] == ["playwright 1.0 (Apache-2.0)"]

    seen: list[tuple[int, str]] = []
    st = movie_pack.install(
        comp["url"],
        expected_size=comp["size"],
        expected_sha256=comp["sha256"],
        progress=lambda p, m: seen.append((p, m)),
    )
    assert st["usable"]
    assert seen[0][0] == 0 and seen[-1] == (100, "movie components installed")
    assert any("downloading" in m for _p, m in seen)
    assert not list(movie_pack.root().glob(".download-*"))


def test_lookup_says_offline_no_release_and_no_pack(host, monkeypatch):
    srv, folder = host
    assert movie_pack.lookup()["reason"] == "offline"  # no releases.json yet
    (folder.parent / "releases.json").write_text(json.dumps({"schema": 1, "releases": []}))
    assert movie_pack.lookup()["reason"] == "no-release"
    idx = _index(srv.url, "radarx-movie-pack.zip", 10, "a" * 64, python="cpython-27")
    (folder.parent / "releases.json").write_text(json.dumps(idx))
    assert movie_pack.lookup()["reason"] == "no-pack"
    monkeypatch.setenv(movie_pack.ENV_BASE_URL, "http://127.0.0.1:1")
    out = movie_pack.lookup()
    assert out["reason"] == "offline" and "could not read" in out["message"]


def test_download_refuses_wrong_size_wrong_hash_and_another_host(host):
    srv, folder = host
    name = movie_pack.pack_name(VERSION)
    zip_path = make_pack(folder / name)
    url = f"{srv.url}/radarx/v{VERSION}/{name}"
    size, digest = zip_path.stat().st_size, sha(zip_path)
    with pytest.raises(movie_pack.MoviePackError, match="announces"):
        movie_pack.install(url, expected_size=size + 1, expected_sha256=digest)
    with pytest.raises(movie_pack.MoviePackError, match="SHA-256"):
        movie_pack.install(url, expected_size=size, expected_sha256="b" * 64)
    with pytest.raises(movie_pack.MoviePackError, match="refusing to download"):
        movie_pack.install(
            f"http://127.0.0.1:{srv.srv.server_address[1] + 1}/x.zip",
            expected_size=size,
            expected_sha256=digest,
        )
    with pytest.raises(movie_pack.MoviePackError, match="size and SHA-256"):
        movie_pack.install(url)
    assert not movie_pack.status()["installed"]
    assert not list(movie_pack.root().glob(".*"))


def test_cancel_stops_the_download(host):
    srv, folder = host
    name = movie_pack.pack_name(VERSION)
    zip_path = make_pack(folder / name)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(movie_pack.MoviePackCancelledError):
        movie_pack.install(
            f"{srv.url}/radarx/v{VERSION}/{name}",
            expected_size=zip_path.stat().st_size,
            expected_sha256=sha(zip_path),
            cancel=cancel,
        )
    assert not movie_pack.status()["installed"]


# --- the Movie Studio messages ---------------------------------------------------------------------


class _Stub:
    session = None

    def __init__(self):
        self.out: list[dict] = []

    async def send(self, msg):
        self.out.append(msg)


@pytest.mark.asyncio
async def test_websocket_messages_status_install_remove(host, monkeypatch):
    from engine import animate_service as svc

    class Conn(svc.AnimateMessages, _Stub):
        pass

    srv, folder = host
    name = movie_pack.pack_name(VERSION)
    zip_path = make_pack(folder / name)
    (folder.parent / "releases.json").write_text(
        json.dumps(_index(srv.url, name, zip_path.stat().st_size, sha(zip_path)))
    )
    c = Conn()
    # the installed app has neither library in its engine (a development venv may have both)
    monkeypatch.setattr(animate_render, "video_status", lambda: {"playwright": False, "av": False})
    await c._handle_animate_capabilities({"request_id": "c0"})
    caps = c.out[-1]
    assert caps["movie_pack"]["needed"] and not caps["movie_pack"]["installed"]
    assert any(p.get("movie_pack") for p in caps["problems"])

    await c._handle_movie_pack_status({"request_id": "s1", "lookup": True})
    st = c.out[-1]
    assert st["type"] == "movie_pack_status" and st["available"]["ok"]
    comp = st["available"]["component"]
    assert st["available"]["consent"]["size"] == comp["size"]

    await c._handle_movie_pack_install({"request_id": "i1", **comp})
    kinds = [m["type"] for m in c.out]
    assert "movie_pack_progress" in kinds and c.out[-1]["type"] == "movie_pack_installed"
    assert c.out[-1]["status"]["usable"]

    await c._handle_movie_pack_install({"request_id": "i2", "url": comp["url"]})
    assert c.out[-1]["type"] == "movie_pack_error" and "size" in c.out[-1]["message"]

    await c._handle_movie_pack_remove({"request_id": "r1"})
    assert c.out[-1]["type"] == "movie_pack_removed" and not c.out[-1]["status"]["installed"]


@pytest.mark.asyncio
async def test_web_mode_refuses_the_pack_messages(pack_env):
    from engine import animate_service as svc

    class Conn(svc.AnimateMessages, _Stub):
        session = object()

    c = Conn()
    for msg in ({"type": "movie_pack_status"}, {"type": "movie_pack_remove"}):
        await c._handle_movie_pack_status(msg)
    assert all(m["type"] == "animate_error" for m in c.out)
