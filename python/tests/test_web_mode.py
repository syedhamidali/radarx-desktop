"""Web mode: the engine as a public, anonymous service (engine/web.py, docs/WEB.md).

Security is the point of this file: every handler that takes a path is attacked, sessions are
checked for isolation, uploads/downloads for abuse. Desktop behaviour must stay unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import matplotlib.pyplot  # noqa: F401  (import on the main thread: the export worker would otherwise do it)
import pytest
from websockets.datastructures import Headers

import server
from engine import mosaic, pipeline, plugins, varmap, web
from engine import multidoppler as mdop
from engine.reader import RadarReader
from tests.conftest import make_synthetic_datatree
from tests.web_helpers import (
    ALLOWED_ORIGIN,
    FakeWS,
    WebClient,
    close_session,
    make_netcdf,
    make_session,
    wait_for,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Web mode on, small limits, every 'home' redirected into tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    env = {
        "XRADAR_WEB": "1",
        "XRADAR_WEB_TMP": str(tmp_path / "webtmp"),
        "XRADAR_ALLOWED_ORIGINS": ALLOWED_ORIGIN,
        "XRADAR_MAX_UPLOAD_MB": "1",
        "XRADAR_MAX_SESSION_MB": "3",
        "XRADAR_MAX_SESSIONS": "2",
        "XRADAR_MAX_OPEN_FILES": "2",
        "XRADAR_IDLE_TIMEOUT_S": "1800",
        "XRADAR_DOWNLOAD_TTL_S": "600",
        "XRADAR_RATE_BURST": "1000",
        "XRADAR_RATE_PER_S": "1000",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    web.reload_settings()
    yield tmp_path
    for ctx in list(web.manager().sessions.values()):
        server._end_web_session(ctx)
    monkeypatch.undo()
    web.reload_settings()


@pytest.fixture()
async def client(web_env: Path):
    c = WebClient()
    yield c
    c.close()


@pytest.fixture()
async def two_clients(web_env: Path):
    a, b = WebClient(), WebClient()
    yield a, b
    a.close()
    b.close()


@pytest.fixture()
def nc_file(tmp_path: Path) -> Path:
    return make_netcdf(tmp_path / "scan.nc")


def _no_outside_writes(tmp_path: Path) -> None:
    """Nothing the attack tests try may create files outside webtmp / the planted sentinels."""
    assert not (tmp_path / "pwned").exists()
    assert not list((tmp_path / "home").rglob("*"))


# ---------------------------------------------------------------------------
# Settings and web_info
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in list(os.environ):
            if key.startswith("XRADAR_"):
                monkeypatch.delenv(key)
        cfg = web.reload_settings()
        assert cfg.enabled is False
        assert (cfg.max_upload_mb, cfg.max_session_mb, cfg.max_sessions) == (200, 600, 8)
        assert (cfg.max_open_files, cfg.idle_timeout_s, cfg.download_ttl_s) == (3, 1800, 600)
        assert cfg.allowed_origins == ()
        monkeypatch.undo()
        web.reload_settings()

    def test_env_parsing_and_bad_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XRADAR_WEB", "true")
        monkeypatch.setenv("XRADAR_MAX_UPLOAD_MB", "garbage")
        monkeypatch.setenv("XRADAR_MAX_SESSIONS", "-5")
        monkeypatch.setenv("XRADAR_ALLOWED_ORIGINS", " https://A.example/ , https://b.example ")
        cfg = web.reload_settings()
        assert cfg.enabled and cfg.max_upload_mb == 200 and cfg.max_sessions == 1
        assert cfg.allowed_origins == ("https://a.example", "https://b.example")
        monkeypatch.undo()
        web.reload_settings()

    def test_web_info_content(self, web_env: Path) -> None:
        info = web.web_info()
        assert info == {
            "type": "web_info",
            "web_mode": True,
            "limits": {
                "max_upload_mb": 1,
                "max_sessions": 2,
                "idle_timeout_s": 1800,
                "max_open_files": 2,
            },
            "features": {"uploads": True, "cloud": True, "local_paths": False, "downloads": True},
        }

    def test_web_defaults_do_not_override_explicit_values(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XRADAR_MODULE_TIMEOUT_S", raising=False)
        monkeypatch.delenv("XRADAR_MAX_OPEN_MB", raising=False)
        web.apply_web_defaults(system_memory_mb=8192)
        assert os.environ["XRADAR_MODULE_TIMEOUT_S"] == "120"
        assert int(os.environ["XRADAR_MAX_OPEN_MB"]) >= 400
        monkeypatch.setenv("XRADAR_MODULE_TIMEOUT_S", "7")
        web.apply_web_defaults(system_memory_mb=8192)
        assert os.environ["XRADAR_MODULE_TIMEOUT_S"] == "7"
        monkeypatch.delenv("XRADAR_MODULE_TIMEOUT_S")
        monkeypatch.delenv("XRADAR_MAX_OPEN_MB")


# ---------------------------------------------------------------------------
# Connection lifecycle: web_info, origins, capacity, idle, rate limit
# ---------------------------------------------------------------------------


class TestConnection:
    async def _run(self, ws: FakeWS, script: Any = None) -> None:
        task = asyncio.get_running_loop().create_task(server._handle_connection(ws))
        if script is not None:
            await script(ws)
        else:
            ws.finish()
        await asyncio.wait_for(task, 10)

    async def test_web_info_sent_first_on_connect(self, web_env: Path) -> None:
        ws = FakeWS()
        await self._run(ws)
        assert ws.sent[0] == web.web_info()

    async def test_session_dir_exists_while_connected_and_is_removed_after(
        self, web_env: Path
    ) -> None:
        ws = FakeWS()

        async def script(ws: FakeWS) -> None:
            await wait_for(lambda: bool(ws.sent))
            sessions = list(web.manager().sessions.values())
            assert len(sessions) == 1 and sessions[0].dir.is_dir()
            assert oct(sessions[0].dir.stat().st_mode & 0o777) == "0o700"
            script.dir = sessions[0].dir  # type: ignore[attr-defined]
            ws.finish()

        await self._run(ws, script)
        assert not script.dir.exists()  # type: ignore[attr-defined]
        assert not web.manager().sessions

    async def test_no_session_token_required(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(server, "EXPECTED_SESSION_TOKEN", "s3cret")
        ws = FakeWS()

        async def script(ws: FakeWS) -> None:
            ws.feed({"type": "ping"})
            await wait_for(lambda: "pong" in ws.types())
            ws.finish()

        await self._run(ws, script)
        assert "error" not in ws.types()

    @pytest.mark.parametrize(
        "origin", ["https://evil.example", "null", None, "http://app.example.com"]
    )
    async def test_bad_or_missing_origin_closes_1008(
        self, web_env: Path, origin: str | None
    ) -> None:
        ws = FakeWS(origin=origin)
        await self._run(ws)
        assert ws.closed is not None and ws.closed[0] == 1008
        assert ws.sent == []  # not even web_info
        assert not web.manager().sessions

    async def test_explicit_star_allows_any_origin(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_ALLOWED_ORIGINS", "*")
        web.reload_settings()
        ws = FakeWS(origin="https://anything.example")
        await self._run(ws)
        assert ws.types()[0] == "web_info" and ws.closed is None

    async def test_same_origin_when_no_allow_list(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XRADAR_ALLOWED_ORIGINS")
        web.reload_settings()
        ok = FakeWS(origin="https://svc.example.com", host="svc.example.com")
        await self._run(ok)
        assert ok.types()[0] == "web_info"
        other = FakeWS(origin="https://app.example.com", host="svc.example.com")
        await self._run(other)
        assert other.closed is not None and other.closed[0] == 1008

    async def test_max_sessions_refuses_extra_connections(self, web_env: Path) -> None:
        a, b, c = FakeWS(), FakeWS(), FakeWS()
        ta = asyncio.get_running_loop().create_task(server._handle_connection(a))
        tb = asyncio.get_running_loop().create_task(server._handle_connection(b))
        await wait_for(lambda: len(web.manager().sessions) == 2)
        await self._run(c)  # third connection while two are open
        assert c.closed is not None and c.closed[0] == 1013
        assert c.sent[0]["type"] == "error" and c.sent[0]["code"] == "server_busy"
        assert "capacity" in c.sent[0]["message"]
        assert len(web.manager().sessions) == 2
        a.finish()
        b.finish()
        await asyncio.gather(ta, tb)
        assert not web.manager().sessions
        d = FakeWS()  # a slot is free again
        await self._run(d)
        assert d.types()[0] == "web_info"

    async def test_idle_timeout_closes_and_cleans_up(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_IDLE_TIMEOUT_S", "1")
        web.reload_settings()
        ws = FakeWS()
        task = asyncio.get_running_loop().create_task(server._handle_connection(ws))
        await wait_for(lambda: bool(web.manager().sessions))
        folder = next(iter(web.manager().sessions.values())).dir
        await asyncio.wait_for(task, 10)  # nobody sends anything: the watcher closes it
        assert ws.closed == (1000, "Idle timeout")
        assert not folder.exists() and not web.manager().sessions

    async def test_ping_does_not_keep_session_alive(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_IDLE_TIMEOUT_S", "1")
        web.reload_settings()
        ws = FakeWS()
        task = asyncio.get_running_loop().create_task(server._handle_connection(ws))
        await wait_for(lambda: bool(ws.sent))

        async def pinger() -> None:
            while not task.done():
                ws.feed({"type": "ping"})
                await asyncio.sleep(0.1)

        pt = asyncio.get_running_loop().create_task(pinger())
        await asyncio.wait_for(task, 10)
        pt.cancel()
        assert ws.closed == (1000, "Idle timeout")

    async def test_rate_limit(self, web_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XRADAR_RATE_BURST", "5")
        monkeypatch.setenv("XRADAR_RATE_PER_S", "0.1")
        web.reload_settings()
        ws = FakeWS()

        async def script(ws: FakeWS) -> None:
            for _ in range(20):
                ws.feed({"type": "ping"})
            await wait_for(lambda: ws.types().count("pong") + ws.types().count("error") >= 20)
            ws.finish()

        await self._run(ws, script)
        assert ws.types().count("pong") == 5
        limited = [m for m in ws.sent if m.get("code") == "rate_limited"]
        assert len(limited) == 15

    def test_rate_limiter_refills(self) -> None:
        rl = web.RateLimiter(10.0, 2)
        rl.stamp = 0.0
        assert rl.allow(0.0) and rl.allow(0.0) and not rl.allow(0.0)
        assert rl.allow(0.2)  # 2 tokens refilled over 0.2 s at 10/s; one used
        assert rl.consecutive_denied == 0

    async def test_desktop_connection_sends_nothing_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XRADAR_WEB", raising=False)
        web.reload_settings()
        ws = FakeWS(origin=None)
        task = asyncio.get_running_loop().create_task(server._handle_connection(ws))
        await asyncio.sleep(0.05)
        ws.finish()
        await asyncio.wait_for(task, 5)
        assert ws.sent == []


# ---------------------------------------------------------------------------
# Desktop mode is unchanged
# ---------------------------------------------------------------------------


class TestDesktopUnchanged:
    def _handler(self):
        ws = FakeWS(origin=None)
        handler = server.ConnectionHandler(ws)  # type: ignore[arg-type]
        return handler, ws

    async def test_get_web_info_reports_false(self) -> None:
        assert not web.is_web_mode()
        handler, ws = self._handler()
        await handler.handle_message(json.dumps({"type": "get_web_info"}))
        assert ws.sent == [{"type": "web_info", "web_mode": False}]

    async def test_upload_messages_do_not_exist(self) -> None:
        handler, ws = self._handler()
        await handler.handle_message(json.dumps({"type": "upload_start", "upload_id": "x"}))
        assert ws.sent[-1]["type"] == "error"
        assert "Unknown message type" in ws.sent[-1]["message"]

    async def test_local_paths_still_open(self, nc_file: Path) -> None:
        handler, ws = self._handler()
        before = dict(server._file_readers)
        await handler.handle_message(json.dumps({"type": "open_file", "path": str(nc_file)}))
        opened = [m for m in ws.sent if m["type"] == "file_opened"]
        assert opened
        server._file_readers.clear()
        server._file_readers.update(before)

    async def test_token_still_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "EXPECTED_SESSION_TOKEN", "s3cret")
        handler, ws = self._handler()
        await handler.handle_message(json.dumps({"type": "ping"}))
        assert ws.sent[-1] == {"type": "error", "message": "Unauthorized WebSocket message"}
        await handler.handle_message(json.dumps({"type": "ping", "token": "s3cret"}))
        assert ws.sent[-1]["type"] == "pong"

    def test_module_dirs_and_stores_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dirs = plugins.module_dirs()
        assert Path.home() / ".xradar-desktop" / "modules" in dirs
        monkeypatch.setenv("XRADAR_MODULES_DIR", "/somewhere")
        assert Path("/somewhere") in plugins.module_dirs()
        monkeypatch.delenv("XRADAR_RECIPES_DIR", raising=False)
        assert pipeline.recipes_dir() == Path.home() / ".xradar-desktop" / "recipes"
        monkeypatch.delenv("XRADAR_VARMAP_FILE", raising=False)
        assert varmap.store_path() == Path.home() / ".xradar-desktop" / "variable_maps.json"

    def test_state_accessors_use_module_globals(self) -> None:
        assert server._readers() is server._file_readers
        assert server._overrides_store() is server._var_overrides
        assert server._renderer() is server._shared_renderer
        assert server._mdop_runs() is server._multidoppler_runs

    async def test_export_with_explicit_path_still_works(self, tmp_path: Path) -> None:
        handler, ws = self._handler()
        reader = RadarReader()
        reader._dtree = make_synthetic_datatree(n_azimuth=12, n_range=20)
        reader._format = "synthetic"
        fid = server._register_reader(reader)
        try:
            out = tmp_path / "plain.csv"
            await handler.handle_message(
                json.dumps({"type": "export", "format": "csv", "path": str(out), "file_id": fid})
            )
            done = [m for m in ws.sent if m["type"] == "export_complete"]
            assert done and done[0]["path"] == str(out) and "download_url" not in done[0]
            assert out.is_file()
        finally:
            server._file_readers.pop(fid, None)
            server._generations.pop(fid, None)
            server._last_file_id = next(reversed(server._file_readers), None)


# ---------------------------------------------------------------------------
# Path guard
# ---------------------------------------------------------------------------


def attack_paths(ctx: web.SessionContext, tmp_path: Path) -> list[str]:
    """Inputs that must never resolve to a readable server path."""
    d = str(ctx.dir)
    secret = tmp_path / "secret.nc"
    secret.write_bytes(b"outside")
    uploads = ctx.uploads_dir
    (uploads / "link.nc").symlink_to(secret)  # symlink escape (file)
    (uploads / "linkdir").symlink_to(tmp_path)  # symlink escape (directory)
    other = make_session()
    (other.uploads_dir / "theirs.nc").write_bytes(b"other visitor")
    paths = [
        "/etc/passwd",
        "/etc/passwd\x00.nc",
        "../../../etc/passwd",
        d + "/../../../etc/passwd",
        d + "/uploads/../../secret.nc",
        d + "/../secret.nc",
        "~/secret.nc",
        "~root/.ssh/id_rsa",
        "file:///etc/passwd",
        "FILE:///etc/passwd",
        "s3://some-bucket/key.nc",
        "http://example.com/x.nc",
        "https://example.com/x.nc",
        "icechunk://nexrad-arco/KTLX/VCP-12",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "\\\\server\\share\\x.nc",
        "//etc/passwd",
        "",
        "   ",
        ".",
        "..",
        "uploads/relative.nc",
        "./uploads/relative.nc",
        d,  # the session root itself
        str(tmp_path),  # its parent
        str(ctx.dir.parent),  # the shared web tmp root
        str(uploads / "link.nc"),
        str(uploads / "linkdir" / "secret.nc"),
        str(other.uploads_dir / "theirs.nc"),  # another visitor's upload
        str(other.dir),
        d + "x",  # sibling that merely shares the prefix
        d + "/" + "a/" * 3000,
    ]
    attack_paths.other = other  # type: ignore[attr-defined]
    return paths


class TestPathGuard:
    def test_every_attack_is_rejected(self, web_env: Path) -> None:
        ctx = make_session()
        try:
            for raw in attack_paths(ctx, web_env):
                with pytest.raises(web.WebError, match="disabled"):
                    ctx.resolve_input_path(raw)
            for bad in (None, 5, b"/x", ["/a"], {"a": 1}, True):
                with pytest.raises(web.WebError):
                    ctx.resolve_input_path(bad)
        finally:
            close_session(attack_paths.other)  # type: ignore[attr-defined]
            close_session(ctx)

    def test_a_session_file_is_accepted_and_symlink_inside_is_resolved(self, web_env: Path) -> None:
        ctx = make_session()
        try:
            f = ctx.uploads_dir / "ok.nc"
            f.write_bytes(b"x")
            assert ctx.resolve_input_path(str(f)) == os.path.realpath(f)
            (ctx.uploads_dir / "alias.nc").symlink_to(f)  # stays inside: fine, resolves to target
            assert ctx.resolve_input_path(str(ctx.uploads_dir / "alias.nc")) == os.path.realpath(f)
        finally:
            close_session(ctx)

    async def test_open_file_rejects_every_attack_and_never_touches_the_reader(
        self, client: WebClient, web_env: Path
    ) -> None:
        attacks = attack_paths(client.ctx, web_env)
        try:
            with patch.object(
                RadarReader, "open_file", side_effect=AssertionError("reader must not be reached")
            ):
                for raw in attacks:
                    replies = await client.call({"type": "open_file", "path": raw})
                    errors = [m for m in replies if m["type"] == "error"]
                    assert errors, raw
                    assert "disabled" in errors[-1]["message"] or "path" in errors[-1]["message"]
                    assert not [m for m in replies if m["type"] == "file_opened"]
            assert not client.ctx.state.file_readers
        finally:
            close_session(attack_paths.other)  # type: ignore[attr-defined]

    async def test_error_does_not_reveal_whether_a_path_exists(
        self, client: WebClient, web_env: Path
    ) -> None:
        a = (await client.call({"type": "open_file", "path": "/etc/passwd"}))[-1]
        b = (await client.call({"type": "open_file", "path": "/definitely/not/there"}))[-1]
        assert a["message"] == b["message"]
        assert "Opening files from the server is disabled" in a["message"]

    async def test_inside_missing_file_is_a_clean_error(self, client: WebClient) -> None:
        missing = str(client.ctx.uploads_dir / "nope.nc")
        replies = await client.call({"type": "open_file", "path": missing})
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "File not found" in errors[-1]["message"]
        assert str(client.ctx.dir) not in json.dumps(replies)

    async def test_server_paths_are_scrubbed_from_errors(self, client: WebClient) -> None:
        bad = client.ctx.uploads_dir / "bad.nc"
        bad.write_bytes(b"not a radar file at all" * 20)
        replies = await client.call({"type": "open_file", "path": str(bad)})
        text = json.dumps(replies)
        assert "file_opened" not in text and "error" in text
        assert str(client.ctx.dir) not in text and client.ctx.real_dir not in text


# ---------------------------------------------------------------------------
# Every path-taking handler is attacked
# ---------------------------------------------------------------------------

BAD_OUTPUT_NAMES = [
    "/tmp/pwned/out.png",
    "../pwned/out.png",
    "..",
    "../../pwned",
    "sub/out.png",
    "sub\\out.png",
    "~/pwned.png",
    "~",
    "file:///tmp/pwned.png",
    "C:\\pwned.png",
    "name:with:colon.png",
    ".hidden.png",
    "a\x00b.png",
    "",
    "  ",
    "x" * 300,
]


class TestEveryPathHandler:
    """One test per ``msg.get("path"/"output_dir"/...)`` consumer found in server.py."""

    def test_the_handler_inventory_is_complete(self) -> None:
        """Fails when someone adds a path-taking handler without extending this class."""
        source = (Path(server.__file__)).read_text()
        pattern = re.compile(r'msg\.get\("(path|output_dir|dir|file|filename|paths|folder)"')
        found: set[str] = set()
        current: str | None = None
        for line in source.splitlines():
            m = re.match(r"\s+(?:async )?def (\w+)\(", line)
            if m:
                current = m.group(1)
            if current and pattern.search(line):
                found.add(current)
        assert found == {
            "_handle_open_nexrad_l2",  # s3 path
            "_handle_open_file",
            "_handle_upload_start",  # the sanitised upload name (TestUploads)
            "_handle_export",
            "_handle_batch_export",
            "_handle_export_animation",
            "_handle_save_text_file",
            "_handle_save_image_file",  # refused outright in the web service (test below)
            "_export_mosaic",
            "_handle_export_multidoppler",
        }, found

    async def _open(self, client: WebClient, nc_file: Path) -> str:
        return await client.upload_and_open(nc_file)

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES)
    async def test_export_path(
        self, client: WebClient, nc_file: Path, web_env: Path, bad: str
    ) -> None:
        fid = await self._open(client, nc_file)
        replies = await client.call(
            {"type": "export", "format": "png", "path": bad, "file_id": fid, "dpi": 72}
        )
        if bad == "":  # blank = "no name given": a generated name in the session
            assert any(m["type"] == "export_complete" for m in replies)
            return
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "file name only" in errors[-1]["message"], (bad, replies)
        assert not [m for m in replies if m["type"] == "export_complete"]
        _no_outside_writes(web_env)

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES[:-3])
    async def test_batch_export_output_dir(
        self, client: WebClient, nc_file: Path, web_env: Path, bad: str
    ) -> None:
        fid = await self._open(client, nc_file)
        replies = await client.call(
            {
                "type": "batch_export",
                "variables": ["DBZH"],
                "sweeps": [0],
                "format": "png",
                "dpi": 72,
                "output_dir": bad,
                "file_id": fid,
            }
        )
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "file name only" in errors[-1]["message"], (bad, replies)
        _no_outside_writes(web_env)

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES[:-3])
    async def test_export_animation_path(
        self, client: WebClient, nc_file: Path, web_env: Path, bad: str
    ) -> None:
        fid = await self._open(client, nc_file)
        replies = await client.call(
            {
                "type": "export_animation",
                "variable": "DBZH",
                "sweeps": [0, 1],
                "path": bad,
                "dpi": 72,
                "file_id": fid,
            }
        )
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "file name only" in errors[-1]["message"], (bad, replies)
        _no_outside_writes(web_env)

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES[:-3])
    async def test_save_text_file_path(self, client: WebClient, web_env: Path, bad: str) -> None:
        replies = await client.call({"type": "save_text_file", "path": bad, "content": "x"})
        errors = [m for m in replies if m["type"] == "error"]
        assert errors, (bad, replies)
        assert not [m for m in replies if m["type"] == "file_saved"]
        _no_outside_writes(web_env)

    async def test_save_text_file_goes_to_the_session_with_a_download(
        self, client: WebClient
    ) -> None:
        replies = await client.call({"type": "save_text_file", "path": "t.csv", "content": "a,b\n"})
        saved = replies[-1]
        assert saved["type"] == "file_saved" and saved["path"] == "t.csv"
        assert saved["download_url"].startswith("/download/") and saved["download_url"].endswith(
            "/t.csv"
        )
        assert (client.ctx.exports_dir / "t.csv").read_text() == "a,b\n"

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES[:-3])
    async def test_save_image_file_is_refused_in_the_web_service(
        self, client: WebClient, web_env: Path, bad: str
    ) -> None:
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode()
        replies = await client.call({"type": "save_image_file", "path": bad, "data_base64": png})
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "web service" in errors[-1]["message"], (bad, replies)
        assert not [m for m in replies if m["type"] == "file_saved"]
        _no_outside_writes(web_env)

    async def test_save_text_file_size_cap_and_quota(self, client: WebClient) -> None:
        big = "x" * (server.WEB_SAVE_TEXT_MAX_BYTES + 1)
        replies = await client.call({"type": "save_text_file", "path": "big.txt", "content": big})
        assert replies[-1]["type"] == "error" and "exceeds" in replies[-1]["message"]
        assert not list(client.ctx.exports_dir.glob("big*"))

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES[:-3])
    async def test_export_mosaic_path(self, client: WebClient, web_env: Path, bad: str) -> None:
        client.ctx.owned["mosaics"].add("m1")
        replies = await client.call(
            {"type": "export_mosaic", "mosaic_id": "m1", "format": "png", "path": bad}
        )
        errors = [m for m in replies if m["type"] == "mosaic_error"]
        assert errors, (bad, replies)
        assert not [m for m in replies if m["type"] == "file_saved"]
        _no_outside_writes(web_env)

    @pytest.mark.parametrize("bad", BAD_OUTPUT_NAMES[:-3])
    async def test_export_multidoppler_path(
        self, client: WebClient, web_env: Path, bad: str
    ) -> None:
        client.ctx.owned["winds"].add("w1")
        replies = await client.call({"type": "export_multidoppler", "wind_id": "w1", "path": bad})
        errors = [m for m in replies if m["type"] == "multidoppler_error"]
        assert errors, (bad, replies)
        assert not [m for m in replies if m["type"] == "file_saved"]
        _no_outside_writes(web_env)

    @pytest.mark.parametrize(
        "bad",
        [
            "/etc/passwd",
            "file:///etc/passwd",
            "s3://evil-bucket/KTLX20130520_201643_V06",
            "s3://unidata-nexrad-level2/../../etc/passwd",
            "s3://unidata-nexrad-level2/2013/05/20/KTLX/../../../x",
            "s3://unidata-nexrad-level2/",
            "s3://unidata-nexrad-level2",
            "s3://noaa-nexrad-level2.evil.com/x",
            "s3://unidata-nexrad-level2/a b",
            "https://unidata-nexrad-level2.s3.amazonaws.com/x",
            "s3://AKIA:secret@unidata-nexrad-level2/x",
            "~/x",
            "",
        ],
    )
    async def test_open_nexrad_l2_only_the_public_bucket(self, client: WebClient, bad: str) -> None:
        with patch.object(RadarReader, "open_s3_file", side_effect=AssertionError("must not run")):
            replies = await client.call({"type": "open_nexrad_l2", "path": bad})
        assert [m for m in replies if m["type"] == "error"], (bad, replies)
        assert not [m for m in replies if m["type"] == "file_opened"]

    async def test_open_nexrad_l2_public_bucket_works_and_uses_session_scratch(
        self, client: WebClient
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_open(self: RadarReader, s3_path: str) -> dict[str, Any]:
            seen["path"] = s3_path
            seen["tmp_dir"] = self.tmp_dir
            seen["max"] = self.max_decompressed_bytes
            self._dtree = make_synthetic_datatree(n_azimuth=12, n_range=20)
            self._format = "synthetic"
            self._path = s3_path
            return {"source": s3_path, "variables": ["DBZH"], "sweeps": []}

        good = "s3://unidata-nexrad-level2/2013/05/20/KTLX/KTLX20130520_201643_V06"
        with patch.object(RadarReader, "open_s3_file", fake_open):
            replies = await client.call({"type": "open_nexrad_l2", "path": good})
        assert [m for m in replies if m["type"] == "file_opened"], replies
        assert seen["path"] == good
        assert seen["tmp_dir"] == str(client.ctx.dir)  # scratch downloads stay in the session
        assert seen["max"] == client.ctx.settings.max_session_bytes

    @pytest.mark.parametrize(
        ("station", "key"),
        [
            ("KTLX", "../../x"),
            ("KT/X", "VCP-12"),
            ("../..", "VCP-12"),
            ("KTLX", "a/b"),
            ("KTLX", ""),
        ],
    )
    async def test_open_arco_scan_validates_inputs(
        self, client: WebClient, station: str, key: str
    ) -> None:
        with patch.object(RadarReader, "open_arco_scan", side_effect=AssertionError("no")):
            replies = await client.call(
                {"type": "open_arco_scan", "station": station, "scan_key": key}
            )
        assert [m for m in replies if m["type"] == "error"], replies

    async def test_cloud_listing_stays_enabled(self, client: WebClient) -> None:
        with patch("server.list_nexrad_stations", return_value=[{"id": "KTLX"}]):
            replies = await client.call({"type": "list_nexrad_stations"})
        assert replies[-1]["type"] == "nexrad_stations"

    async def test_unknown_path_keys_are_ignored(self, client: WebClient, nc_file: Path) -> None:
        """Extra path-ish keys on any message do nothing (handlers only read their own)."""
        fid = await self._open(client, nc_file)
        replies = await client.call(
            {
                "type": "get_metadata_tree",
                "file_id": fid,
                "path": "/etc/passwd",
                "dir": "/",
                "file": "/etc/passwd",
            }
        )
        assert replies[-1]["type"] == "metadata_tree_result"


# ---------------------------------------------------------------------------
# Module parameters, plug-ins, recipes, variable maps
# ---------------------------------------------------------------------------


class TestPersistenceAndPlugins:
    def test_module_dirs_only_the_bundled_folder(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra = web_env / "evil_modules"
        extra.mkdir()
        monkeypatch.setenv("XRADAR_MODULES_DIR", str(extra))
        (Path.home() / ".xradar-desktop" / "modules").mkdir(parents=True)
        dirs = plugins.module_dirs()
        assert dirs == [Path(plugins.__file__).resolve().parent.parent / "modules"]

    def test_user_modules_are_never_executed(
        self, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = web_env / "pwned"
        for folder in (web_env / "m1", Path.home() / ".xradar-desktop" / "modules"):
            folder.mkdir(parents=True)
            (folder / "evil.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
        monkeypatch.setenv("XRADAR_MODULES_DIR", str(web_env / "m1"))
        plugins.discover_modules(extra_dirs=[web_env / "m1"])
        assert not marker.exists()
        assert all("evil" not in (m.source or "") for m in plugins.MODULES.values())
        monkeypatch.undo()
        web.reload_settings()
        plugins.discover_modules()

    async def test_list_modules_hides_server_paths_and_ignores_reload(
        self, client: WebClient
    ) -> None:
        with patch.object(
            plugins, "discover_modules", side_effect=AssertionError("no rescans")
        ) as rescan:
            replies = await client.call({"type": "list_modules", "reload": True})
        assert not rescan.called
        listing = replies[-1]
        assert listing["type"] == "modules_list" and listing["dirs"] == []
        for err in listing["errors"]:
            assert "/" not in err["file"]

    def test_module_path_params_must_be_session_files(self, client: WebClient) -> None:
        h = client.handler
        inside = client.ctx.uploads_dir / "dem.nc"
        inside.write_bytes(b"x")
        assert h._web_module_params("m", {"dem_path": str(inside), "n": 3}) == {
            "dem_path": os.path.realpath(inside),
            "n": 3,
        }
        assert h._web_module_params("m", {"dem_path": "", "threshold": 1}) == {
            "dem_path": "",
            "threshold": 1,
        }
        for key, value in [
            ("dem_path", "/etc/passwd"),
            ("dem_path", "../../etc/passwd"),
            ("dem_path", "~/dem.nc"),
            ("dem_path", "file:///etc/passwd"),
            ("input_file", "/etc/passwd"),
            ("out_dir", "/tmp"),
            ("path", "/etc/passwd"),
            ("model_path", "/etc/passwd"),
            ("model_path", str(inside)),  # an ML model is never user-supplied
        ]:
            with pytest.raises(plugins.ModuleError):
                h._web_module_params("m", {key: value})
        # names that merely contain the letters are not paths
        assert h._web_module_params("m", {"profile": "x", "pathology": 1}) == {
            "profile": "x",
            "pathology": 1,
        }

    async def test_run_module_with_a_path_param_is_refused(
        self, client: WebClient, nc_file: Path
    ) -> None:
        fid = await client.upload_and_open(nc_file)

        @plugins.module(
            id="web_test_mod",
            title="t",
            description="d",
            params=[
                plugins.param("dem_path", "string", ""),
            ],
        )
        def web_test_mod(ctx, dem_path):  # pragma: no cover - must never run
            raise AssertionError("module must not run")

        try:
            replies = await client.call(
                {
                    "type": "run_module",
                    "module": "web_test_mod",
                    "sweep": 0,
                    "params": {"dem_path": "/etc/passwd"},
                    "file_id": fid,
                }
            )
        finally:
            plugins.MODULES.pop("web_test_mod", None)
        errors = [m for m in replies if m["type"] == "module_error"]
        assert errors and "disabled" in errors[0]["message"]

    async def test_recipes_live_in_the_session_not_home(
        self, client: WebClient, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = web_env / "elsewhere"
        monkeypatch.setenv("XRADAR_RECIPES_DIR", str(elsewhere))
        recipe = {
            "name": "My QC",
            "target": "DBZH",
            "steps": [{"step_id": "snr_gate", "params": {"min_snr": 3.0}}],
        }
        replies = await client.call({"type": "save_recipe", "recipe": recipe})
        types = [m["type"] for m in replies]
        assert "recipe_saved" in types, replies
        listing = [m for m in replies if m["type"] == "recipes_list"][-1]
        assert listing["dir"] == ""
        assert any(r["name"] == "My QC" for r in listing["recipes"])
        assert list((client.ctx.dir / "recipes").glob("*.json"))
        assert not elsewhere.exists()
        assert not (Path.home() / ".xradar-desktop").exists()

    async def test_recipes_are_not_shared_between_sessions(
        self, two_clients: tuple[WebClient, WebClient]
    ) -> None:
        a, b = two_clients
        recipe = {"name": "Private", "target": "DBZH", "steps": [{"step_id": "snr_gate"}]}
        await a.call({"type": "save_recipe", "recipe": recipe})
        listed = (await b.call({"type": "list_recipes"}))[-1]
        assert all(r["name"] != "Private" for r in listed["recipes"])
        listed_a = (await a.call({"type": "list_recipes"}))[-1]
        assert any(r["name"] == "Private" for r in listed_a["recipes"])
        await b.call({"type": "delete_recipe", "name": "Private"})
        assert any(
            r["name"] == "Private" for r in (await a.call({"type": "list_recipes"}))[-1]["recipes"]
        )

    async def test_recipe_count_is_capped(self, client: WebClient) -> None:
        for i in range(50):
            await client.call(
                {
                    "type": "save_recipe",
                    "recipe": {
                        "name": f"r{i:03d}",
                        "target": "DBZH",
                        "steps": [{"step_id": "snr_gate"}],
                    },
                }
            )
        replies = await client.call(
            {
                "type": "save_recipe",
                "recipe": {
                    "name": "one-too-many",
                    "target": "DBZH",
                    "steps": [{"step_id": "snr_gate"}],
                },
            }
        )
        errors = [m for m in replies if m["type"] == "pipeline_error"]
        assert errors and "Too many saved recipes" in errors[0]["message"]

    def test_recipes_dir_requires_a_session_in_web_mode(self, web_env: Path) -> None:
        with pytest.raises(plugins.ModuleError):
            pipeline.recipes_dir()

    async def test_variable_mapping_is_memory_only(
        self, client: WebClient, nc_file: Path, web_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = web_env / "varmaps.json"
        monkeypatch.setenv("XRADAR_VARMAP_FILE", str(store))
        fid = await client.upload_and_open(nc_file)
        replies = await client.call(
            {"type": "set_variable_mapping", "file_id": fid, "mapping": {"reflectivity": "DBZH"}}
        )
        assert replies[-1]["type"] == "variable_mapping"
        assert replies[-1]["overrides"]
        assert not store.exists()
        assert not (Path.home() / ".xradar-desktop").exists()
        # engine-level safety net: even a direct call cannot persist in web mode
        varmap.save_persisted("/x/y.nc", {"reflectivity": "DBZH"})
        assert not store.exists()
        assert varmap.load_saved("/x/y.nc") == {}

    async def test_variable_mapping_not_shared_between_sessions(
        self, two_clients: tuple[WebClient, WebClient], nc_file: Path
    ) -> None:
        a, b = two_clients
        fa = await a.upload_and_open(nc_file)
        fb = await b.upload_and_open(nc_file)
        await a.call(
            {"type": "set_variable_mapping", "file_id": fa, "mapping": {"reflectivity": "VRADH"}}
        )
        mb = (await b.call({"type": "get_variable_mapping", "file_id": fb}))[-1]
        assert not mb["overrides"]
        ma = (await a.call({"type": "get_variable_mapping", "file_id": fa}))[-1]
        assert ma["overrides"]

    def test_default_export_dir_refused_in_web_mode(self, web_env: Path) -> None:
        from engine.exporter import default_export_dir

        with pytest.raises(ValueError):
            default_export_dir()

    def test_no_process_spawning_anywhere_in_the_engine(self) -> None:
        root = Path(server.__file__).parent
        files = [
            root / "server.py",
            *(root / "engine").glob("*.py"),
            *(root / "modules").glob("*.py"),
        ]
        offenders = [
            (path.name, needle)
            for path in files
            for needle in (
                "subprocess",
                "os.system",
                "Popen(",
                "multiprocessing.Process",
                "os.exec",
            )
            if needle in path.read_text()
        ]
        assert not offenders, offenders


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


class TestUploads:
    async def test_happy_path_upload_then_open_and_render(
        self, client: WebClient, nc_file: Path
    ) -> None:
        data = nc_file.read_bytes()
        done = await client.upload(data, "KTLX test.nc", chunk=7000)
        assert done["type"] == "upload_complete" and done["upload_id"] == "u1"
        path = Path(done["path"])
        assert path.is_file() and path.read_bytes() == data
        assert client.ctx.contains(os.path.realpath(path))
        assert path.name == "KTLX_test.nc"  # sanitised, extension kept
        progress = [m for m in client.sent if m["type"] == "upload_progress"]
        assert progress[-1]["received"] == len(data)
        assert [m["received"] for m in progress] == sorted(m["received"] for m in progress)
        opened = [
            m
            for m in await client.call({"type": "open_file", "path": done["path"]})
            if m["type"] == "file_opened"
        ]
        assert opened and opened[0]["data"]["variables"] == ["DBZH", "VRADH"]
        fid = opened[0]["file_id"]
        render = await client.call(
            {
                "type": "render",
                "variable": "DBZH",
                "sweep": 0,
                "width": 64,
                "height": 64,
                "file_id": fid,
            }
        )
        assert "render_result" in [m["type"] for m in render]
        await client.call(
            {"type": "get_sweep_data", "variable": "DBZH", "sweep": 1, "file_id": fid}
        )
        assert client.binary  # raster payload went out
        assert not list(client.ctx.uploads_dir.rglob("*.part"))

    async def test_gzip_upload_opens_and_scratch_stays_in_session(
        self, client: WebClient, nc_file: Path
    ) -> None:
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("tmp*"))
        gz = gzip.compress(nc_file.read_bytes())
        done = await client.upload(gz, "scan.nc.gz")
        assert done["type"] == "upload_complete"
        replies = await client.call({"type": "open_file", "path": done["path"]})
        assert [m for m in replies if m["type"] == "file_opened"], replies
        assert set(Path(tempfile.gettempdir()).glob("tmp*")) == before
        # A NetCDF/HDF5 tree reads from its decompressed copy while the file is open, so the copy
        # stays -- inside the session folder -- and goes away with the session.
        assert len(list(client.ctx.dir.glob("tmp*"))) == 1
        client.close()
        assert not client.ctx.dir.exists()

    async def test_gzip_bomb_is_refused_without_filling_the_disk(self, client: WebClient) -> None:
        bomb = gzip.compress(b"\0" * (6 * 1024 * 1024), 9)  # 6 MB of zeros, ~6 KB gzipped
        assert len(bomb) < 100_000
        done = await client.upload(bomb, "bomb.gz")
        assert done["type"] == "upload_complete"
        replies = await client.call({"type": "open_file", "path": done["path"]})
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "decompressed" in errors[-1]["message"]
        assert client.ctx.used_bytes() < client.ctx.settings.max_session_bytes
        assert not list(client.ctx.dir.glob("tmp*"))

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\boot.ini", "boot.ini"),
            ("/abs/path/scan.nc", "scan.nc"),
            ("a b&c;d|e.nc", "a_b_c_d_e.nc"),
            (".bashrc", "bashrc"),
            ("..", "upload"),
            ("...", "upload"),
            ("", "upload"),
            ("   ", "upload"),
            ("\x00evil.nc", "_evil.nc"),
            ("scan\u202e.nc", "scan_.nc"),
            ("KTLX20130520_201643_V06.gz", "KTLX20130520_201643_V06.gz"),
            ("café.nc", "caf_.nc"),
            ("a" * 400 + ".nc", None),
        ],
    )
    async def test_filename_sanitising(
        self, client: WebClient, raw: str, expected: str | None
    ) -> None:
        done = await client.upload(b"hello", raw, upload_id="n1")
        assert done["type"] == "upload_complete", done
        path = Path(done["path"])
        assert client.ctx.contains(os.path.realpath(path))
        assert path.parent.parent == client.ctx.uploads_dir
        assert "/" not in path.name and "\\" not in path.name and not path.name.startswith(".")
        if expected is None:
            assert path.name.endswith(".nc") and len(path.name) <= 120
        else:
            assert path.name == expected

    async def test_non_string_filename(self, client: WebClient) -> None:
        for bad in (None, 5, ["a"], {"x": 1}):
            r = (
                await client.call(
                    {"type": "upload_start", "upload_id": "f", "filename": bad, "size": 3}
                )
            )[-1]
            assert r["type"] == "upload_ready", bad
            await client.call({"type": "upload_abort", "upload_id": "f"})

    @pytest.mark.parametrize("size", [0, -1, 1.5, "10", None, True, [10], 2 * 1024 * 1024])
    async def test_bad_declared_size(self, client: WebClient, size: Any) -> None:
        r = (
            await client.call(
                {"type": "upload_start", "upload_id": "s", "filename": "a.nc", "size": size}
            )
        )[-1]
        assert r["type"] == "upload_error" and r["upload_id"] == "s"
        assert not client.ctx.uploads

    @pytest.mark.parametrize("uid", ["", "a/b", "../x", "x" * 65, "a b", 5, None, "é"])
    async def test_bad_upload_ids(self, client: WebClient, uid: Any) -> None:
        r = (
            await client.call(
                {"type": "upload_start", "upload_id": uid, "filename": "a.nc", "size": 5}
            )
        )[-1]
        assert r["type"] == "upload_error"
        assert not client.ctx.uploads

    async def test_exactly_at_the_limit_is_fine_one_over_is_not(self, client: WebClient) -> None:
        limit = client.ctx.settings.max_upload_bytes
        ok = await client.call(
            {"type": "upload_start", "upload_id": "a", "filename": "a.bin", "size": limit}
        )
        assert ok[-1]["type"] == "upload_ready"
        await client.call({"type": "upload_abort", "upload_id": "a"})
        over = await client.call(
            {"type": "upload_start", "upload_id": "b", "filename": "b.bin", "size": limit + 1}
        )
        assert over[-1]["type"] == "upload_error" and "limit" in over[-1]["message"]

    async def test_more_data_than_declared(self, client: WebClient) -> None:
        r = await client.upload(b"x" * 100, "a.nc", declared_size=60, chunk=50)
        assert r["type"] == "upload_error" and "more data" in r["message"]
        assert not client.ctx.uploads
        assert not list(client.ctx.uploads_dir.rglob("*"))  # partial file removed

    async def test_fewer_data_than_declared_fails_at_end(self, client: WebClient) -> None:
        r = await client.upload(b"x" * 40, "a.nc", declared_size=100)
        assert r["type"] == "upload_error" and "incomplete" in r["message"]
        assert not client.ctx.uploads and not list(client.ctx.uploads_dir.rglob("*"))

    async def test_chunk_ordering_enforced(self, client: WebClient) -> None:
        await client.call({"type": "upload_start", "upload_id": "o", "filename": "a", "size": 30})

        def chunk(seq: Any, raw: bytes = b"0123456789") -> dict[str, Any]:
            return {
                "type": "upload_chunk",
                "upload_id": "o",
                "seq": seq,
                "data": base64.b64encode(raw).decode(),
            }

        assert (await client.call(chunk(0)))[-1]["type"] == "upload_progress"
        skipped = (await client.call(chunk(2)))[-1]
        assert skipped["type"] == "upload_error" and "Out-of-order" in skipped["message"]
        # a protocol violation aborts the upload: the next (correct) chunk has nothing to attach to
        assert (await client.call(chunk(1)))[-1]["type"] == "upload_error"
        assert not client.ctx.uploads

    @pytest.mark.parametrize("seq", [0, -1, 1.0, "1", None, True])
    async def test_duplicate_or_weird_seq(self, client: WebClient, seq: Any) -> None:
        await client.call({"type": "upload_start", "upload_id": "o", "filename": "a", "size": 30})
        first = {
            "type": "upload_chunk",
            "upload_id": "o",
            "data": base64.b64encode(b"abcde").decode(),
        }
        assert (await client.call({**first, "seq": 0}))[-1]["type"] == "upload_progress"
        r = (await client.call({**first, "seq": seq}))[-1]
        assert r["type"] == "upload_error"  # replay of 0 or nonsense: aborted

    async def test_pipelined_chunks_are_applied_in_order(self, client: WebClient) -> None:
        """The server runs each message as a task; chunks sent back-to-back must not reorder."""
        await client.call(
            {"type": "upload_start", "upload_id": "p", "filename": "a.bin", "size": 40}
        )
        parts = [bytes([65 + i]) * 10 for i in range(4)]
        msgs = [
            json.dumps(
                {
                    "type": "upload_chunk",
                    "upload_id": "p",
                    "seq": i,
                    "data": base64.b64encode(p).decode(),
                }
            )
            for i, p in enumerate(parts)
        ]
        tasks = [
            asyncio.get_running_loop().create_task(client.handler.handle_message(m)) for m in msgs
        ]
        await asyncio.gather(*tasks)
        r = (await client.call({"type": "upload_end", "upload_id": "p"}))[-1]
        assert r["type"] == "upload_complete"
        assert Path(r["path"]).read_bytes() == b"".join(parts)

    async def test_invalid_base64_and_oversized_chunk(self, client: WebClient) -> None:
        await client.call(
            {"type": "upload_start", "upload_id": "b", "filename": "a", "size": 900000}
        )
        base = {"type": "upload_chunk", "upload_id": "b", "seq": 0}
        r = (await client.call({**base, "data": "!!!not base64!!!"}))[-1]
        assert r["type"] == "upload_error" and "base64" in r["message"]
        # a fresh upload for the oversized-chunk case (the invalid one was aborted)
        await client.call(
            {"type": "upload_start", "upload_id": "b", "filename": "a", "size": 900000}
        )
        big = base64.b64encode(b"x" * (web.MAX_CHUNK_BYTES + 1)).decode()
        r = (await client.call({**base, "data": big}))[-1]
        assert r["type"] == "upload_error" and "larger" in r["message"]
        for bad in (None, 5, ["x"], ""):
            await client.call(
                {"type": "upload_start", "upload_id": "b", "filename": "a", "size": 90}
            )
            r = (await client.call({**base, "data": bad}))[-1]
            assert r["type"] == "upload_error", bad

    async def test_exactly_one_mib_chunk_is_accepted(self, client: WebClient) -> None:
        size = web.MAX_CHUNK_BYTES
        await client.call(
            {"type": "upload_start", "upload_id": "m", "filename": "a.bin", "size": size}
        )
        r = (
            await client.call(
                {
                    "type": "upload_chunk",
                    "upload_id": "m",
                    "seq": 0,
                    "data": base64.b64encode(b"z" * size).decode(),
                }
            )
        )[-1]
        assert r["type"] == "upload_progress" and r["received"] == size

    async def test_duplicate_concurrent_upload_id(self, client: WebClient) -> None:
        start = {"type": "upload_start", "upload_id": "d", "filename": "a.bin", "size": 10}
        assert (await client.call(start))[-1]["type"] == "upload_ready"
        dup = (await client.call({**start, "filename": "other.bin"}))[-1]
        assert dup["type"] == "upload_error" and "already in progress" in dup["message"]
        # the first upload is unharmed
        r = await client.call(
            {
                "type": "upload_chunk",
                "upload_id": "d",
                "seq": 0,
                "data": base64.b64encode(b"0123456789").decode(),
            }
        )
        assert r[-1]["type"] == "upload_progress"
        assert (await client.call({"type": "upload_end", "upload_id": "d"}))[-1][
            "type"
        ] == "upload_complete"
        # once finished the id may be reused
        assert (await client.call(start))[-1]["type"] == "upload_ready"

    async def test_too_many_parallel_uploads(self, client: WebClient) -> None:
        for i in range(2):
            r = await client.call(
                {"type": "upload_start", "upload_id": f"p{i}", "filename": "a", "size": 10}
            )
            assert r[-1]["type"] == "upload_ready"
        r = await client.call(
            {"type": "upload_start", "upload_id": "p2", "filename": "a", "size": 10}
        )
        assert r[-1]["type"] == "upload_error" and "Too many" in r[-1]["message"]

    async def test_chunk_and_end_without_start(self, client: WebClient) -> None:
        r = (
            await client.call(
                {"type": "upload_chunk", "upload_id": "ghost", "seq": 0, "data": "QQ=="}
            )
        )[-1]
        assert r["type"] == "upload_error"
        r = (await client.call({"type": "upload_end", "upload_id": "ghost"}))[-1]
        assert r["type"] == "upload_error"
        await client.call({"type": "upload_abort", "upload_id": "ghost"})  # silently ignored

    async def test_abort_removes_partial_files(self, client: WebClient) -> None:
        await client.call(
            {"type": "upload_start", "upload_id": "x", "filename": "a.nc", "size": 20}
        )
        await client.call(
            {
                "type": "upload_chunk",
                "upload_id": "x",
                "seq": 0,
                "data": base64.b64encode(b"1234").decode(),
            }
        )
        assert list(client.ctx.uploads_dir.rglob("*.part"))
        await client.call({"type": "upload_abort", "upload_id": "x"})
        assert not list(client.ctx.uploads_dir.rglob("*.part"))
        assert not [p for p in client.ctx.uploads_dir.rglob("*") if p.is_file()]
        assert not client.ctx.uploads

    async def test_session_quota_across_uploads(self, client: WebClient) -> None:
        # quota 3 MB, per-file limit 1 MB: the fourth full-size upload must not fit
        blob = b"q" * (1024 * 1024)
        results = []
        for i in range(4):
            results.append((await client.upload(blob, f"f{i}.bin", upload_id=f"q{i}"))["type"])
        assert results == ["upload_complete"] * 3 + ["upload_error"]
        assert client.ctx.used_bytes() <= client.ctx.settings.max_session_bytes

    async def test_quota_counts_declared_but_unsent_bytes(self, client: WebClient) -> None:
        start = {"type": "upload_start", "filename": "a.bin", "size": 1024 * 1024}
        assert (await client.call({**start, "upload_id": "a"}))[-1]["type"] == "upload_ready"
        assert (await client.call({**start, "upload_id": "b"}))[-1]["type"] == "upload_ready"
        # two reservations of 1 MB each + a third would be 3 MB + ... only 2 uploads allowed anyway,
        # so check the accounting directly
        assert client.ctx.pending_bytes() == 2 * 1024 * 1024
        with pytest.raises(web.WebError, match="quota"):
            client.ctx.check_quota(2 * 1024 * 1024)

    async def test_uploads_go_through_the_rate_limiter_and_connection_limits(
        self, web_env: Path
    ) -> None:
        ws = FakeWS()

        async def script(ws: FakeWS) -> None:
            await wait_for(lambda: bool(ws.sent))
            ws.feed({"type": "upload_start", "upload_id": "z", "filename": "a.nc", "size": 3})
            ws.feed({"type": "upload_chunk", "upload_id": "z", "seq": 0, "data": "QUJD"})
            ws.feed({"type": "upload_end", "upload_id": "z"})
            await wait_for(lambda: "upload_complete" in ws.types())
            ws.finish()

        task = asyncio.get_running_loop().create_task(server._handle_connection(ws))
        await script(ws)
        await asyncio.wait_for(task, 5)
        # chunks pipelined as separate frames are applied in order over the real connection loop
        assert ws.types() == ["web_info", "upload_ready", "upload_progress", "upload_complete"]
        done = ws.sent[-1]
        assert Path(done["path"]).name == "a.nc" and done["size"] == 3

    # -- crafted files ------------------------------------------------------

    def _hdf5_with_external_link(self, tmp_path: Path) -> bytes:
        h5py = pytest.importorskip("h5py")
        path = tmp_path / "ext.h5"
        with h5py.File(path, "w") as f:
            f["ok"] = [1, 2, 3]
            f["evil"] = h5py.ExternalLink("/etc/passwd", "/")
        return path.read_bytes()

    def _hdf5_with_external_storage(self, tmp_path: Path) -> bytes:
        h5py = pytest.importorskip("h5py")
        path = tmp_path / "extds.h5"
        with h5py.File(path, "w") as f:
            f.create_dataset("evil", shape=(4,), dtype="i1", external=[("/etc/hostname", 0, 4)])
        return path.read_bytes()

    def _hdf5_with_virtual_dataset(self, tmp_path: Path) -> bytes:
        h5py = pytest.importorskip("h5py")
        path = tmp_path / "vds.h5"
        layout = h5py.VirtualLayout(shape=(4,), dtype="i1")
        layout[0:4] = h5py.VirtualSource("/etc/hostname", "x", shape=(4,))
        with h5py.File(path, "w") as f:
            f.create_virtual_dataset("evil", layout)
        return path.read_bytes()

    @pytest.mark.parametrize("maker", ["link", "storage", "virtual"])
    async def test_hdf5_files_referencing_outside_data_are_rejected(
        self, client: WebClient, tmp_path: Path, maker: str
    ) -> None:
        data = {
            "link": self._hdf5_with_external_link,
            "storage": self._hdf5_with_external_storage,
            "virtual": self._hdf5_with_virtual_dataset,
        }[maker](tmp_path)
        r = await client.upload(data, "crafted.nc")
        assert r["type"] == "upload_error" and "outside itself" in r["message"]
        assert not list(client.ctx.uploads_dir.rglob("crafted*"))

    async def test_plain_hdf5_passes_the_scan(self, client: WebClient, tmp_path: Path) -> None:
        h5py = pytest.importorskip("h5py")
        path = tmp_path / "plain.h5"
        with h5py.File(path, "w") as f:
            f.create_group("g")["d"] = [1, 2, 3]
            f["soft"] = h5py.SoftLink("/g/d")
        r = await client.upload(path.read_bytes(), "plain.nc")
        assert r["type"] == "upload_complete"


# ---------------------------------------------------------------------------
# Exports and downloads
# ---------------------------------------------------------------------------


class TestExportsAndDownloads:
    async def test_export_lands_in_the_session_with_a_download_url(
        self, client: WebClient, nc_file: Path
    ) -> None:
        fid = await client.upload_and_open(nc_file)
        replies = await client.call(
            {"type": "export", "format": "csv", "file_id": fid, "variable": "DBZH", "sweep": 0}
        )
        done = [m for m in replies if m["type"] == "export_complete"][-1]
        assert "/" not in done["path"] and done["path"].endswith(".csv")
        assert re.fullmatch(r"/download/[A-Za-z0-9_-]{20,}/[\w.-]+\.csv", done["download_url"])
        assert (client.ctx.exports_dir / done["path"]).is_file()
        assert "derived_exported" in done
        assert str(client.ctx.dir) not in json.dumps(replies)  # no server paths in the reply

    async def test_requested_bare_name_is_used_and_not_overwritten(
        self, client: WebClient, nc_file: Path
    ) -> None:
        fid = await client.upload_and_open(nc_file)
        msg = {"type": "export", "format": "csv", "file_id": fid, "path": "my out.csv", "sweep": 0}
        first = [m for m in await client.call(msg) if m["type"] == "export_complete"][-1]
        second = [m for m in await client.call(msg) if m["type"] == "export_complete"][-1]
        assert first["path"] == "my_out.csv" and second["path"] == "my_out-2.csv"

    async def test_name_without_extension_gets_one(self, client: WebClient, nc_file: Path) -> None:
        fid = await client.upload_and_open(nc_file)
        done = [
            m
            for m in await client.call(
                {"type": "export", "format": "csv", "file_id": fid, "path": "volume"}
            )
            if m["type"] == "export_complete"
        ][-1]
        assert done["path"] == "volume.csv"

    @pytest.mark.parametrize("fmt", ["png", "svg", "pdf", "csv"])
    async def test_all_downloadable_formats_work(
        self, client: WebClient, nc_file: Path, fmt: str
    ) -> None:
        fid = await client.upload_and_open(nc_file)
        replies = await client.call(
            {"type": "export", "format": fmt, "file_id": fid, "variable": "DBZH", "dpi": 72}
        )
        done = [m for m in replies if m["type"] == "export_complete"]
        assert done, replies
        res = await web.handle_http("GET", done[-1]["download_url"], Headers({}))
        assert res.status_code == 200 and len(res.body) > 0
        assert (client.ctx.exports_dir / done[-1]["path"]).read_bytes() == res.body

    async def test_zarr_and_high_dpi_refused(self, client: WebClient, nc_file: Path) -> None:
        fid = await client.upload_and_open(nc_file)
        for extra in ({"format": "zarr"}, {"format": "png", "dpi": 600}):
            replies = await client.call({"type": "export", "file_id": fid, **extra})
            assert replies[-1]["type"] == "error"
        assert not list(client.ctx.exports_dir.glob("*"))

    async def test_failed_export_leaves_no_partial_file(
        self, client: WebClient, nc_file: Path
    ) -> None:
        fid = await client.upload_and_open(nc_file)
        with patch.object(server.RadarExporter, "export", side_effect=RuntimeError("boom")):
            replies = await client.call({"type": "export", "format": "png", "file_id": fid})
        assert [m for m in replies if m["type"] == "error"]
        assert not list(client.ctx.exports_dir.glob("*"))

    async def test_batch_and_animation_downloads(self, client: WebClient, nc_file: Path) -> None:
        fid = await client.upload_and_open(nc_file)
        batch = [
            m
            for m in await client.call(
                {
                    "type": "batch_export",
                    "variables": ["DBZH", "VRADH"],
                    "sweeps": [0, 1],
                    "format": "png",
                    "dpi": 72,
                    "file_id": fid,
                    "output_dir": "my batch",
                }
            )
            if m["type"] == "batch_export_complete"
        ][-1]
        assert batch["count"] == 4 and len(batch["paths"]) == len(batch["download_urls"]) == 4
        assert all("/" not in p for p in batch["paths"]) and batch["output_dir"] == "my_batch"
        for url in batch["download_urls"]:
            assert (await web.handle_http("GET", url, Headers({}))).status_code == 200
        anim = [
            m
            for m in await client.call(
                {
                    "type": "export_animation",
                    "variable": "DBZH",
                    "sweeps": [0, 1],
                    "dpi": 72,
                    "file_id": fid,
                }
            )
            if m["type"] == "animation_export_complete"
        ][-1]
        assert anim["path"].endswith(".gif") and "/" not in anim["path"]
        res = await web.handle_http("GET", anim["download_url"], Headers({}))
        assert res.status_code == 200 and res.body[:3] == b"GIF"

    async def test_batch_size_cap(self, client: WebClient, nc_file: Path) -> None:
        fid = await client.upload_and_open(nc_file)
        replies = await client.call(
            {
                "type": "batch_export",
                "variables": ["DBZH"] * 1 + [f"v{i}" for i in range(70)],
                "sweeps": [0],
                "format": "png",
                "file_id": fid,
            }
        )
        assert replies[-1]["type"] == "error" and "at most" in replies[-1]["message"]

    async def test_export_quota(self, client: WebClient, nc_file: Path) -> None:
        fid = await client.upload_and_open(nc_file)
        (client.ctx.exports_dir / "filler.bin").write_bytes(
            b"0" * client.ctx.settings.max_session_bytes
        )
        replies = await client.call({"type": "export", "format": "csv", "file_id": fid})
        assert replies[-1]["type"] == "error" and "quota" in replies[-1]["message"]

    # -- download tokens ----------------------------------------------------

    async def _download(self, client: WebClient, nc_file: Path) -> tuple[str, str]:
        fid = await client.upload_and_open(nc_file)
        done = [
            m
            for m in await client.call({"type": "export", "format": "csv", "file_id": fid})
            if m["type"] == "export_complete"
        ][-1]
        return done["path"], done["download_url"]

    async def test_download_response_headers(self, client: WebClient, nc_file: Path) -> None:
        name, url = await self._download(client, nc_file)
        expected = (client.ctx.exports_dir / name).read_bytes()
        res = await web.handle_http(
            "GET", url, Headers({"Origin": ALLOWED_ORIGIN, "Host": "svc.example.com"})
        )
        assert res.status_code == 200 and res.body == expected
        h = res.headers
        assert h["Content-Length"] == str(len(expected))
        assert (
            h["Content-Disposition"].startswith("attachment;") and name in h["Content-Disposition"]
        )
        assert h["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["Cache-Control"] == "no-store"

    async def test_download_without_origin_has_no_cors_header(
        self, client: WebClient, nc_file: Path
    ) -> None:
        _, url = await self._download(client, nc_file)
        res = await web.handle_http("GET", url, Headers({}))
        assert res.status_code == 200 and "Access-Control-Allow-Origin" not in res.headers

    async def test_download_from_a_foreign_origin_is_forbidden(
        self, client: WebClient, nc_file: Path
    ) -> None:
        _, url = await self._download(client, nc_file)
        res = await web.handle_http("GET", url, Headers({"Origin": "https://evil.example"}))
        assert res.status_code == 403 and b"csv" not in res.body

    async def test_download_wrong_filename_or_token(self, client: WebClient, nc_file: Path) -> None:
        name, url = await self._download(client, nc_file)
        _, _, token, _ = url.split("/")
        # A token that differs in its last character. (A fixed "x" was the real token one time in
        # 64, so the request below was a valid download and the test failed at random.)
        altered = token[:-1] + ("a" if token[-1] != "a" else "b")
        assert altered != token
        for bad in (
            f"/download/{token}/other.csv",
            f"/download/{token}/",
            f"/download/{token}",
            f"/download/{token}/{name}/extra",
            f"/download/{altered}/{name}",
            f"/download/nope/{name}",
            f"/download/{token}/..%2F..%2Fetc%2Fpasswd",
            f"/download/{token}/../../../etc/passwd",
            "/download/",
            "/download",
            "/",
            "/etc/passwd",
        ):
            res = await web.handle_http("GET", bad, Headers({}))
            assert res.status_code == 404, bad

    async def test_download_expires(self, client: WebClient, nc_file: Path) -> None:
        _, url = await self._download(client, nc_file)
        token = url.split("/")[2]
        assert (await web.handle_http("GET", url, Headers({}))).status_code == 200
        web.manager().downloads[token].expires = time.monotonic() - 1
        assert (await web.handle_http("GET", url, Headers({}))).status_code == 404
        assert token not in web.manager().downloads

    async def test_download_ttl_uses_the_setting(
        self, client: WebClient, nc_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, url = await self._download(client, nc_file)
        entry = web.manager().downloads[url.split("/")[2]]
        ttl = entry.expires - time.monotonic()
        assert 590 < ttl <= 600

    async def test_tokens_are_random_and_long(self, client: WebClient, nc_file: Path) -> None:
        tokens = set()
        for i in range(5):
            f = client.ctx.exports_dir / f"f{i}.txt"
            f.write_text("x")
            tokens.add(client.ctx.register_download(f).split("/")[2])
        assert len(tokens) == 5 and all(len(t) >= 30 for t in tokens)

    async def test_session_isolation_of_downloads(
        self, two_clients: tuple[WebClient, WebClient], nc_file: Path
    ) -> None:
        a, b = two_clients
        name_a, url_a = await self._download(a, nc_file)
        # B cannot mint a token for A's file ...
        with pytest.raises(web.WebError):
            b.ctx.register_download(a.ctx.exports_dir / name_a)
        # ... nor for any other server file
        for path in ("/etc/passwd", str(nc_file)):
            with pytest.raises(web.WebError):
                b.ctx.register_download(path)
        # B's own tokens only serve B's files
        mine = b.ctx.exports_dir / "mine.txt"
        mine.write_text("b-secret")
        url_b = b.ctx.register_download(mine)
        assert (await web.handle_http("GET", url_b, Headers({}))).body == b"b-secret"
        # a token never serves another name than the one it was minted for
        token_b = url_b.split("/")[2]
        assert (
            await web.handle_http("GET", f"/download/{token_b}/{name_a}", Headers({}))
        ).status_code == 404
        # closing A removes A's tokens (and files) but not B's
        close_session(a.ctx)
        assert (await web.handle_http("GET", url_a, Headers({}))).status_code == 404
        assert (await web.handle_http("GET", url_b, Headers({}))).status_code == 200

    async def test_download_never_serves_outside_the_session_dir(
        self, client: WebClient, web_env: Path
    ) -> None:
        secret = web_env / "secret.txt"
        secret.write_text("top secret")
        # a symlink inside the session pointing out: cannot even be registered ...
        link = client.ctx.exports_dir / "link.txt"
        link.symlink_to(secret)
        with pytest.raises(web.WebError):
            client.ctx.register_download(link)
        # ... and a legitimately registered file swapped for a symlink later is not served
        real = client.ctx.exports_dir / "real.txt"
        real.write_text("fine")
        url = client.ctx.register_download(real)
        assert (await web.handle_http("GET", url, Headers({}))).body == b"fine"
        real.unlink()
        real.symlink_to(secret)
        assert (await web.handle_http("GET", url, Headers({}))).status_code == 404
        # a forged registry entry pointing outside is refused as well
        token = "forged-token-0123456789abcdef"
        web.manager().downloads[token] = web._DownloadEntry(
            client.ctx.id, str(secret), "secret.txt", time.monotonic() + 100
        )
        assert (
            await web.handle_http("GET", f"/download/{token}/secret.txt", Headers({}))
        ).status_code == 404

    async def test_directories_cannot_be_downloaded(self, client: WebClient) -> None:
        with pytest.raises(web.WebError):
            client.ctx.register_download(client.ctx.exports_dir)

    async def test_session_cleanup_removes_files_and_tokens(
        self, client: WebClient, nc_file: Path
    ) -> None:
        _, url = await self._download(client, nc_file)
        folder = client.ctx.dir
        assert folder.is_dir() and any(folder.rglob("*.csv"))
        client.close()
        assert not folder.exists()
        assert not web.manager().downloads
        assert (await web.handle_http("GET", url, Headers({}))).status_code == 404
        client.close()  # idempotent


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


class TestHttp:
    async def test_health(self, web_env: Path) -> None:
        res = await web.handle_http("GET", "/health", Headers({}))
        assert res.status_code == 200 and res.body == b"ok"
        assert res.headers["Content-Type"].startswith("text/plain")

    async def test_health_with_allowed_origin_gets_cors(self, web_env: Path) -> None:
        res = await web.handle_http("GET", "/health", Headers({"Origin": ALLOWED_ORIGIN}))
        assert res.headers["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
        assert res.headers["Vary"] == "Origin"

    async def test_disallowed_origin_403_everywhere(self, web_env: Path) -> None:
        for path in ("/health", "/download/x/y", "/", "/nothing"):
            res = await web.handle_http("GET", path, Headers({"Origin": "https://evil.example"}))
            assert res.status_code == 403 and "Access-Control-Allow-Origin" not in res.headers

    @pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
    async def test_other_methods_405(self, web_env: Path, method: str) -> None:
        res = await web.handle_http(method, "/health", Headers({}))
        assert res.status_code == 405 and res.headers["Allow"] == "GET"

    async def test_unknown_path_404_and_upgrade_passes(self, web_env: Path) -> None:
        assert (await web.handle_http("GET", "/secret", Headers({}))).status_code == 404
        up = Headers({"Upgrade": "websocket", "Origin": ALLOWED_ORIGIN})
        assert await web.handle_http("GET", "/", up) is None
        up_bad = Headers({"Upgrade": "websocket", "Origin": "https://evil.example"})
        assert (await web.handle_http("GET", "/", up_bad)).status_code == 403
        up_none = Headers({"Upgrade": "websocket"})  # no Origin on a WS upgrade: refused
        assert (await web.handle_http("GET", "/", up_none)).status_code == 403

    def test_cors_header_value(self, web_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = web.settings()
        assert web.cors_origin(ALLOWED_ORIGIN, cfg) == ALLOWED_ORIGIN
        assert web.cors_origin("https://evil.example", cfg) is None
        assert web.cors_origin(None, cfg) is None
        monkeypatch.setenv("XRADAR_ALLOWED_ORIGINS", "*")
        assert web.cors_origin("https://x.example", web.reload_settings()) == "*"

    def test_origin_table(self, web_env: Path) -> None:
        cfg = web.settings()
        ok = web.origin_allowed
        assert ok(ALLOWED_ORIGIN, "h", cfg, allow_missing=False)
        assert ok(ALLOWED_ORIGIN + "/", "h", cfg, allow_missing=False)
        assert ok(ALLOWED_ORIGIN.upper(), "h", cfg, allow_missing=False)
        assert not ok("https://app.example.com.evil.io", "h", cfg, allow_missing=True)
        assert not ok("https://evilapp.example.com", "h", cfg, allow_missing=True)
        assert not ok("null", "h", cfg, allow_missing=True)
        assert ok(None, "h", cfg, allow_missing=True) and not ok(
            None, "h", cfg, allow_missing=False
        )


@pytest.mark.parametrize("sample", ["a.nc", "a b.nc", "é.nc"])
def test_content_disposition_is_header_safe(sample: str) -> None:
    value = web._content_disposition(sample)
    assert "\n" not in value and value.startswith("attachment;")
    assert "filename*=UTF-8''" in value


# ---------------------------------------------------------------------------
# Session isolation and limits
# ---------------------------------------------------------------------------


class TestIsolation:
    async def test_state_is_per_connection(
        self, two_clients: tuple[WebClient, WebClient], nc_file: Path
    ) -> None:
        a, b = two_clients
        before = dict(server._file_readers)
        fa = await a.upload_and_open(nc_file)
        assert fa in a.ctx.state.file_readers and fa not in b.ctx.state.file_readers
        assert dict(server._file_readers) == before  # nothing leaked into the desktop globals
        assert a.ctx.state.renderer is not b.ctx.state.renderer
        assert a.ctx.state.var_overrides is not b.ctx.state.var_overrides
        # B cannot address A's file
        for msg in (
            {"type": "render", "variable": "DBZH", "file_id": fa, "width": 32, "height": 32},
            {"type": "get_sweep_data", "variable": "DBZH", "file_id": fa},
            {"type": "get_variable_mapping", "file_id": fa},
            {"type": "close_file", "file_id": fa},
            {"type": "export", "format": "csv", "file_id": fa},
            {"type": "list_derived", "file_id": fa},
        ):
            replies = await b.call(msg)
            assert not any(m["type"] in ("render_result", "sweep_data") for m in replies), msg
        assert fa in a.ctx.state.file_readers  # B's close_file did not close A's file
        # ... and without a file_id B sees "no file", not A's file
        ping = (await b.call({"type": "ping"}))[-1]
        assert ping["file_open"] is False
        assert (await a.call({"type": "ping"}))[-1]["file_open"] is True

    async def test_generation_and_last_file_are_per_connection(
        self, two_clients: tuple[WebClient, WebClient], nc_file: Path
    ) -> None:
        a, b = two_clients
        fa = await a.upload_and_open(nc_file)
        fb = await b.upload_and_open(nc_file)
        assert a.ctx.state.last_file_id == fa and b.ctx.state.last_file_id == fb
        await a.call({"type": "process", "pipeline": {"steps": []}, "file_id": fa})
        assert a.ctx.state.generations[fa] >= 0
        assert b.ctx.state.generations[fb] == 0

    async def test_pipeline_and_multidoppler_cancel_flags_are_per_session(
        self, two_clients: tuple[WebClient, WebClient]
    ) -> None:
        a, b = two_clients
        await a.call({"type": "multidoppler_cancel", "request_id": "r1"})
        assert "r1" in a.ctx.state.multidoppler_runs and "r1" not in b.ctx.state.multidoppler_runs
        assert "r1" not in server._multidoppler_runs

    async def test_mosaic_and_wind_results_are_owned_by_their_creator(
        self, two_clients: tuple[WebClient, WebClient]
    ) -> None:
        a, b = two_clients
        a.ctx.owned["mosaics"].add("abc123")
        a.ctx.owned["winds"].add("wind123")
        sentinel_dir = b.ctx.exports_dir
        for client, expect_unknown in ((b, True),):
            r = await client.call(
                {"type": "mosaic_probe", "mosaic_id": "abc123", "lon": 0, "lat": 0}
            )
            assert (
                expect_unknown and r[-1]["type"] == "mosaic_error" and "Unknown" in r[-1]["message"]
            )
            r = await client.call(
                {"type": "export_mosaic", "mosaic_id": "abc123", "format": "png", "path": "m.png"}
            )
            assert r[-1]["type"] == "mosaic_error" and "Unknown" in r[-1]["message"]
            r = await client.call(
                {"type": "export_multidoppler", "wind_id": "wind123", "path": "w.nc"}
            )
            assert r[-1]["type"] == "multidoppler_error" and "Unknown" in r[-1]["message"]
        assert not list(sentinel_dir.glob("*"))

    async def test_owned_results_are_discarded_when_the_session_ends(self, web_env: Path) -> None:
        ctx = make_session()
        ctx.owned["mosaics"].add("zz")
        ctx.owned["winds"].add("yy")
        with (
            patch.object(mosaic.MOSAICS, "discard") as dm,
            patch.object(mdop.WIND_RESULTS, "discard") as dw,
        ):
            close_session(ctx)
        dm.assert_called_once_with("zz")
        dw.assert_called_once_with("yy")

    async def test_closing_a_session_releases_readers_and_memory(
        self, client: WebClient, nc_file: Path
    ) -> None:
        fid = await client.upload_and_open(nc_file)
        reader = client.ctx.state.file_readers[fid]
        client.close()
        assert not client.ctx.state.file_readers and reader.datatree is None

    async def test_max_open_files_per_session(self, client: WebClient, nc_file: Path) -> None:
        f1 = await client.upload_and_open(nc_file)
        await client.upload_and_open(nc_file, "second.nc")
        done = await client.upload(nc_file.read_bytes(), "third.nc", upload_id="u3")
        replies = await client.call({"type": "open_file", "path": done["path"]})
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "At most 2 files" in errors[-1]["message"]
        assert len(client.ctx.state.file_readers) == 2
        await client.call({"type": "close_file", "file_id": f1})
        replies = await client.call({"type": "open_file", "path": done["path"]})
        assert [m for m in replies if m["type"] == "file_opened"]

    async def test_cloud_opens_count_towards_the_open_file_limit(self, client: WebClient) -> None:
        def fake_open(self: RadarReader, s3_path: str) -> dict[str, Any]:
            self._dtree = make_synthetic_datatree(n_azimuth=12, n_range=20)
            self._format = "synthetic"
            return {"source": s3_path, "variables": ["DBZH"], "sweeps": []}

        good = "s3://unidata-nexrad-level2/2013/05/20/KTLX/KTLX20130520_201643_V06"
        with patch.object(RadarReader, "open_s3_file", fake_open):
            for _ in range(2):
                r = await client.call({"type": "open_nexrad_l2", "path": good})
                assert [m for m in r if m["type"] == "file_opened"]
            r = await client.call({"type": "open_nexrad_l2", "path": good})
        assert [m for m in r if m["type"] == "error"]

    async def test_memory_budget_refuses_oversized_files(
        self, client: WebClient, nc_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "0.0001")
        done = await client.upload(nc_file.read_bytes(), "big.nc")
        replies = await client.call({"type": "open_file", "path": done["path"]})
        errors = [m for m in replies if m["type"] == "error"]
        assert errors and "per-session limit" in errors[-1]["message"]
        assert not client.ctx.state.file_readers

    async def test_render_size_is_capped(self, client: WebClient, nc_file: Path) -> None:
        fid = await client.upload_and_open(nc_file)
        replies = await client.call(
            {"type": "render", "variable": "DBZH", "file_id": fid, "width": 8000, "height": 100}
        )
        assert replies[-1]["type"] == "error" and "4096" in replies[-1]["message"]

    def test_state_accessors_fail_closed_without_a_session(self, web_env: Path) -> None:
        """A request that lost its session must not fall back to shared state."""
        with pytest.raises(RuntimeError):
            server._readers()
        with pytest.raises(RuntimeError):
            server._get_reader(None)

    async def test_worker_threads_see_the_session(self, client: WebClient, nc_file: Path) -> None:
        """Executor work resolves the caller's state (the loop's executor copies the context)."""
        seen: list[Any] = []

        async def probe(msg: dict[str, Any]) -> None:
            loop = asyncio.get_running_loop()
            seen.append(await loop.run_in_executor(None, lambda: server._cs()))

        # exercise through a real handler path: render runs get_sweep_data / render in executors
        fid = await client.upload_and_open(nc_file)
        replies = await client.call(
            {"type": "render", "variable": "DBZH", "file_id": fid, "width": 32, "height": 32}
        )
        assert "render_result" in [m["type"] for m in replies]
        # and directly
        token = server._SESSION.set(client.ctx)
        try:
            server._ensure_context_executor(asyncio.get_running_loop())
            await probe({})
        finally:
            server._SESSION.reset(token)
        assert seen[0] is client.ctx.state

    async def test_two_sessions_render_concurrently_without_mixing(
        self, two_clients: tuple[WebClient, WebClient], tmp_path: Path
    ) -> None:
        a, b = two_clients
        fa = await a.upload_and_open(make_netcdf(tmp_path / "a.nc", n_azimuth=24, n_range=40))
        fb = await b.upload_and_open(make_netcdf(tmp_path / "b.nc", n_azimuth=36, n_range=50))
        msg = {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0}
        ra, rb = await asyncio.gather(
            a.call({**msg, "file_id": fa}), b.call({**msg, "file_id": fb})
        )
        ha = next(m for m in ra if m["type"] == "sweep_data")
        hb = next(m for m in rb if m["type"] == "sweep_data")
        assert (ha["n_az"], ha["n_range"]) == (24, 40)
        assert (hb["n_az"], hb["n_range"]) == (36, 50)

    async def test_stale_session_dirs_are_purged(self, web_env: Path) -> None:
        root = Path(web.settings().tmp_root)
        root.mkdir(parents=True, exist_ok=True)
        stale = root / f"{web.SESSION_DIR_PREFIX}old"
        stale.mkdir()
        (stale / "f").write_text("x")
        os.utime(stale, (1, 1))
        fresh = root / f"{web.SESSION_DIR_PREFIX}new"
        fresh.mkdir()
        unrelated = root / "someone-elses-dir"
        unrelated.mkdir()
        os.utime(unrelated, (1, 1))
        assert web.manager().purge_stale_dirs() == 1
        assert not stale.exists() and fresh.exists() and unrelated.exists()

    def test_session_dir_is_private_and_unique(self, web_env: Path) -> None:
        a, b = make_session(), make_session()
        try:
            assert a.dir != b.dir and a.id != b.id
            assert (a.dir.stat().st_mode & 0o077) == 0
            with pytest.raises(web.WebError, match="capacity"):
                web.manager().open_session()
        finally:
            close_session(a)
            close_session(b)
        assert not a.dir.exists() and not b.dir.exists()


class TestFilenameHelpers:
    @pytest.mark.parametrize(
        ("name", "ok"),
        [
            ("a.png", True),
            ("my file.png", True),
            ("a/b", False),
            ("a\\b", False),
            ("..", False),
            (".x", False),
            ("~x", False),
            ("c:x", False),
            ("a\x00", False),
            ("", False),
            (5, False),
            (" a", False),
            ("x" * 256, False),
        ],
    )
    def test_is_bare_name(self, name: Any, ok: bool) -> None:
        assert web.is_bare_name(name) is ok

    def test_export_target_stays_inside_exports(self, web_env: Path) -> None:
        ctx = make_session()
        try:
            for name in ("a.png", "weird name!!.png", "x" * 200 + ".png", "ünï.png"):
                target = ctx.export_target(name, ext="png")
                assert target.parent == ctx.exports_dir
                assert ctx.contains(os.path.realpath(target))
            assert ctx.export_target(None, ext="csv").suffix == ".csv"
        finally:
            close_session(ctx)


# ---------------------------------------------------------------------------
# End to end over a real socket (websockets server + client + raw HTTP)
# ---------------------------------------------------------------------------


async def _http(port: int, request: str) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
    return status, headers, body


class TestEndToEnd:
    async def test_full_flow_over_real_sockets(self, web_env: Path, nc_file: Path) -> None:
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        from websockets.exceptions import InvalidStatus

        server_cm = serve(
            server._handle_connection,
            "127.0.0.1",
            0,
            process_request=web.process_request,
            compression=None,
            max_size=8 * 1024 * 1024,
        )
        async with server_cm as srv:
            port = srv.sockets[0].getsockname()[1]

            status, _, body = await _http(
                port, "GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            assert (status, body) == (200, b"ok")
            status, _, _ = await _http(
                port,
                "GET /health HTTP/1.1\r\nHost: x\r\nOrigin: https://evil.example\r\n"
                "Connection: close\r\n\r\n",
            )
            assert status == 403
            # the websockets parser only accepts GET and drops anything else before our hook
            # (so no 405 is possible on the wire; handle_http covers the 405 logic)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"HEAD /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await writer.drain()
            assert not (await reader.read()).startswith(b"HTTP/1.1 200")
            writer.close()
            status, _, _ = await _http(
                port, "GET /nope HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            assert status == 404

            with pytest.raises(InvalidStatus) as bad:
                async with connect(f"ws://127.0.0.1:{port}", origin="https://evil.example"):
                    pass
            assert bad.value.response.status_code == 403

            async with connect(f"ws://127.0.0.1:{port}", origin=ALLOWED_ORIGIN) as ws:
                info = json.loads(await ws.recv())
                assert info["type"] == "web_info" and info["features"]["local_paths"] is False

                async def rpc(msg: dict[str, Any], until: str) -> dict[str, Any]:
                    await ws.send(json.dumps(msg))
                    while True:
                        frame = await asyncio.wait_for(ws.recv(), 20)
                        if isinstance(frame, str):
                            reply = json.loads(frame)
                            if reply["type"] in (until, "error", "upload_error"):
                                return reply

                data = nc_file.read_bytes()
                ready = await rpc(
                    {
                        "type": "upload_start",
                        "upload_id": "e2e",
                        "filename": "scan.nc",
                        "size": len(data),
                    },
                    "upload_ready",
                )
                assert ready["type"] == "upload_ready"
                for seq, off in enumerate(range(0, len(data), 8192)):
                    prog = await rpc(
                        {
                            "type": "upload_chunk",
                            "upload_id": "e2e",
                            "seq": seq,
                            "data": base64.b64encode(data[off : off + 8192]).decode(),
                        },
                        "upload_progress",
                    )
                    assert prog["type"] == "upload_progress"
                done = await rpc({"type": "upload_end", "upload_id": "e2e"}, "upload_complete")
                assert done["type"] == "upload_complete"

                bad_open = await rpc({"type": "open_file", "path": "/etc/passwd"}, "file_opened")
                assert bad_open["type"] == "error" and "disabled" in bad_open["message"]
                opened = await rpc({"type": "open_file", "path": done["path"]}, "file_opened")
                assert opened["type"] == "file_opened"
                exported = await rpc(
                    {
                        "type": "export",
                        "format": "csv",
                        "file_id": opened["file_id"],
                        "path": "out.csv",
                    },
                    "export_complete",
                )
                assert exported["type"] == "export_complete" and exported["path"] == "out.csv"

                status, headers, body = await _http(
                    port,
                    f"GET {exported['download_url']} HTTP/1.1\r\nHost: x\r\n"
                    f"Origin: {ALLOWED_ORIGIN}\r\nConnection: close\r\n\r\n",
                )
                assert status == 200
                assert headers["content-disposition"].startswith("attachment")
                assert headers["content-length"] == str(len(body))
                assert headers["access-control-allow-origin"] == ALLOWED_ORIGIN
                assert (
                    body
                    == (
                        next(iter(web.manager().sessions.values())).exports_dir / "out.csv"
                    ).read_bytes()
                )
                url = exported["download_url"]
                folder = next(iter(web.manager().sessions.values())).dir

            # connection closed -> session folder and download are gone
            await wait_for(lambda: not web.manager().sessions)
            assert not folder.exists()
            status, _, _ = await _http(
                port, f"GET {url} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            assert status == 404
