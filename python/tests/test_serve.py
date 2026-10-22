"""``rxd serve``: the engine on a cluster for the app on your own computer (docs/cluster.md).

Real WebSocket connections to a real in-process engine on 127.0.0.1; the account gate is the
project's `gate_on` fixture (local test key, mocked account server). Nothing leaves this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from engine import cli, serve
from engine.account import core
from engine.account.store import SessionStore

PYTHON_DIR = Path(__file__).resolve().parents[1]


def make_engine(**kw) -> serve.RemoteEngine:
    config = serve.ServeConfig(
        port=0, idle_minutes=kw.pop("idle_minutes", 0), fail_delay_base_s=0.0, **kw
    )
    return serve.RemoteEngine(config)


class Running:
    """An engine serving in this event loop, stopped (cleanly) when the test ends."""

    def __init__(self, engine: serve.RemoteEngine) -> None:
        self.engine = engine
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> serve.RemoteEngine:
        self.task = asyncio.create_task(self.engine.run())
        await asyncio.wait_for(self.engine.ready.wait(), 30)
        return self.engine

    async def __aexit__(self, *exc) -> None:
        self.engine.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, 15)


def url(engine: serve.RemoteEngine, host: str = "127.0.0.1") -> str:
    return f"ws://{host}:{engine.port}"


async def close_code(ws) -> int:
    """Wait for the server to end the connection; return its close code."""
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 10)
    return ws.close_code


async def logged_in(engine: serve.RemoteEngine):
    ws = await connect(url(engine))
    await ws.send(json.dumps({"type": "engine_auth", "token": engine.token}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
    assert reply["type"] == "engine_auth_ok"
    return ws


async def call(ws, msg: dict) -> dict:
    await ws.send(json.dumps(msg))
    return json.loads(await asyncio.wait_for(ws.recv(), 10))


# -- the address and the token --------------------------------------------------------------------


def test_the_token_is_new_every_time_and_long():
    tokens = {serve.new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 43 for t in tokens)  # 32 random bytes, URL-safe base64


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.20", "example.org", "10.0.0.1", "", " 8.8.8.8 ", "127.0.0.1.evil"],
)
def test_only_this_computer_can_be_bound(host):
    if host == "":
        assert serve.resolve_host(host) == "127.0.0.1"  # nothing given: the safe default
        return
    with pytest.raises(serve.ServeError, match="this computer only"):
        serve.resolve_host(host)


def test_localhost_is_bound_as_the_loopback_address():
    assert serve.resolve_host("localhost") == "127.0.0.1"
    assert serve.resolve_host("LOCALHOST") == "127.0.0.1"
    assert serve.resolve_host(None) == "127.0.0.1"


@pytest.mark.parametrize("port", [-1, 1, 80, 1023, 65536, 70000])
def test_ports_a_user_cannot_use_are_refused(port):
    with pytest.raises(serve.ServeError):
        serve.check_port(port)


def test_there_is_no_switch_to_listen_elsewhere():
    options = (
        cli.build_parser()._subparsers._group_actions[0].choices["serve"]._option_string_actions
    )
    assert not [o for o in options if "local" in o or "public" in o or "all" in o]


def test_the_command_refuses_a_foreign_host_before_anything_starts(capsys):
    assert cli.main(["serve", "--host", "0.0.0.0"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "this computer only" in err and "Traceback" not in err
    assert cli.main(["serve", "--port", "80"]) == cli.EXIT_USAGE
    assert cli.main(["serve", "--idle-minutes", "-3"]) == cli.EXIT_USAGE
    capsys.readouterr()


def test_the_command_needs_a_signed_in_account(gate_on, capsys):
    """No shortcut: without `rxd login` on this computer the command stops with exit code 5."""
    assert cli.main(["serve"]) == cli.EXIT_ACCOUNT
    err = capsys.readouterr().err
    assert "not signed in" in err.lower() and "rxd login" in err


def test_the_banner_shows_the_address_the_token_once_and_the_ssh_line():
    text = serve.banner(
        host="127.0.0.1",
        port=48213,
        token="TOKEN-ABC",
        token_file=None,
        user="ada",
        node="login01",
        idle_minutes=120,
    )
    assert "127.0.0.1:48213" in text
    assert text.count("TOKEN-ABC") == 1
    assert "ssh -L 48213:localhost:48213 ada@login01" in text
    assert "Settings > Engine" in text and "120 minutes" in text
    assert "-J" not in text  # not a batch job


def test_the_banner_for_a_batch_job_adds_the_jump_host_form():
    text = serve.banner(
        host="127.0.0.1", port=5000, token="T", token_file=None, user="ada", node="node042",
        idle_minutes=0, batch=True,
    )  # fmt: skip
    assert "ssh -J ada@LOGIN-NODE -L 5000:localhost:5000 ada@node042" in text
    assert "no idle limit" in text.lower()


def test_the_token_file_is_private_and_the_banner_does_not_print_the_token(tmp_path):
    path = tmp_path / "token"
    path.write_text("old")
    os.chmod(path, 0o644)
    serve.write_token_file(path, "SECRET-1")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text().strip() == "SECRET-1"
    text = serve.banner(
        host="127.0.0.1", port=5000, token=None, token_file=path, user="u", node="n", idle_minutes=5
    )
    assert "SECRET-1" not in text and str(path) in text


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need rights on Windows")
def test_the_token_file_is_never_written_through_a_symbolic_link(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    link = tmp_path / "token"
    link.symlink_to(target)
    with pytest.raises(OSError):
        serve.write_token_file(link, "SECRET")
    assert target.read_text() == "keep"


# -- the connection: token first, nothing else before it ------------------------------------------


async def test_the_right_token_is_accepted_and_the_engine_answers():
    async with Running(make_engine()) as engine:
        ws = await logged_in(engine)
        assert (await call(ws, {"type": "ping"}))["type"] == "pong"
        await ws.close()


@pytest.mark.parametrize(
    "first",
    [
        json.dumps({"type": "ping"}),  # a real request instead of the token
        json.dumps({"type": "engine_auth"}),  # no token
        json.dumps({"type": "engine_auth", "token": "wrong-token"}),
        json.dumps({"type": "engine_auth", "token": ""}),
        json.dumps({"type": "engine_auth", "token": 12345}),
        json.dumps({"type": "engine_auth", "token": None}),
        json.dumps({"type": "engine_auth", "token": ["a"]}),
        json.dumps(["engine_auth"]),
        json.dumps("engine_auth"),
        "not json at all",
        "{" * 5000,
        b"\x00\x01binary",
    ],
    ids=lambda f: str(f)[:28],
)
async def test_a_missing_or_wrong_token_closes_the_connection_with_1008(first):
    async with Running(make_engine()) as engine:
        ws = await connect(url(engine))
        await ws.send(first)
        assert await close_code(ws) == 1008
        assert ws.close_reason == "Authentication failed"  # the same words for every failure


async def test_a_request_before_the_token_is_never_processed():
    """The ping is not answered: the connection is closed on the first message, whatever it is."""
    async with Running(make_engine()) as engine:
        ws = await connect(url(engine))
        await ws.send(json.dumps({"type": "ping"}))
        with pytest.raises(ConnectionClosed):
            reply = await asyncio.wait_for(ws.recv(), 10)
            assert reply is None  # never reached: a pong would be a processed message
        assert ws.close_code == 1008


async def test_silence_is_not_a_way_in():
    async with Running(make_engine(handshake_timeout_s=0.3)) as engine:
        ws = await connect(url(engine))
        assert await close_code(ws) == 1008  # no first message within the time allowed


async def test_a_second_app_is_refused_while_one_is_connected():
    async with Running(make_engine()) as engine:
        first = await logged_in(engine)
        second = await connect(url(engine))
        await second.send(json.dumps({"type": "engine_auth", "token": engine.token}))
        busy = json.loads(await asyncio.wait_for(second.recv(), 10))
        assert busy["code"] == "engine_busy"
        assert await close_code(second) == 1013
        assert (await call(first, {"type": "ping"}))["type"] == "pong"  # the first is undisturbed
        await first.close()
        await asyncio.sleep(0.3)  # the slot is free again
        third = await logged_in(engine)
        await third.close()


async def test_a_socket_that_never_authenticates_does_not_take_the_place():
    async with Running(make_engine(handshake_timeout_s=30)) as engine:
        idle = await connect(url(engine))  # opens, says nothing
        owner = await logged_in(engine)
        assert (await call(owner, {"type": "ping"}))["type"] == "pong"
        await owner.close()
        await idle.close()


async def test_a_flood_of_unauthenticated_sockets_is_turned_away():
    async with Running(make_engine(handshake_timeout_s=30, max_pending=3)) as engine:
        held = [await connect(url(engine)) for _ in range(3)]
        extra = await connect(url(engine))
        assert await close_code(extra) == 1013
        for ws in held:
            await ws.close()
        await asyncio.sleep(0.2)
        owner = await logged_in(engine)  # once the strangers' sockets are gone the owner is in
        await owner.close()


# -- failed attempts are slowed down, a right token never is --------------------------------------


def test_failures_wait_longer_and_longer_and_are_forgotten_after_a_minute():
    now = [0.0]
    limiter = serve.FailureLimiter(base_s=0.25, cap_s=5.0, window_s=60.0, clock=lambda: now[0])
    assert [limiter.record() for _ in range(7)] == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0]
    now[0] = 61.0
    assert limiter.record() == 0.25


async def test_a_failed_attempt_is_slow_and_the_owner_is_not_locked_out():
    engine = make_engine()
    engine.limiter = serve.FailureLimiter(base_s=0.2, cap_s=0.2)
    async with Running(engine):
        loop = asyncio.get_running_loop()
        started = loop.time()
        bad = await connect(url(engine))
        await bad.send(json.dumps({"type": "engine_auth", "token": "nope"}))
        assert await close_code(bad) == 1008
        assert loop.time() - started >= 0.19  # the penalty
        for _ in range(5):
            wrong = await connect(url(engine))
            await wrong.send(json.dumps({"type": "engine_auth", "token": "nope"}))
            await close_code(wrong)
        owner = await logged_in(engine)  # still in at once with the right token
        assert (await call(owner, {"type": "ping"}))["type"] == "pong"
        await owner.close()


# -- who may knock: Host and Origin ---------------------------------------------------------------


async def http_status(port: int, host: str, origin: str | None = None) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [
        "GET / HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
    ]
    if origin:
        lines.append(f"Origin: {origin}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    status = (await asyncio.wait_for(reader.readline(), 10)).decode()
    writer.close()
    return status.strip()


async def test_a_page_that_resolves_a_foreign_name_to_this_port_is_refused():
    async with Running(make_engine()) as engine:
        assert "403" in await http_status(engine.port, "evil.example")
        assert "403" in await http_status(engine.port, f"evil.example:{engine.port}")
        assert "403" in await http_status(engine.port, "127.0.0.1.evil.example")
        assert "101" in await http_status(engine.port, f"localhost:{engine.port}")
        assert "101" in await http_status(engine.port, "127.0.0.1:9999")  # any forwarded port


@pytest.mark.parametrize(
    "origin,allowed",
    [
        (None, True),
        ("tauri://localhost", True),
        ("http://tauri.localhost", True),
        ("https://tauri.localhost", True),
        ("http://localhost:1420", True),
        ("https://evil.example", False),
        ("http://localhost.evil.example", False),
        ("tauri://localhost.evil.example", False),
        ("http://127.0.0.1.evil.example:80", False),
        ("null", False),
        ("http://user@localhost:1", False),
    ],
)
def test_only_the_app_itself_may_open_a_connection_from_a_web_page(origin, allowed):
    assert serve.origin_ok(origin) is allowed


async def test_a_foreign_web_page_cannot_reach_the_engine_even_with_the_token():
    async with Running(make_engine()) as engine:
        with pytest.raises(InvalidStatus) as err:
            await connect(url(engine), origin="https://evil.example")
        assert err.value.response.status_code == 403


# -- nothing leaks: not into logs, not into texts -------------------------------------------------


async def test_the_token_is_in_no_log_line_and_no_error_text(caplog, capsys):
    caplog.set_level(
        logging.DEBUG
    )  # the most talkative setting: the websockets library logs frames
    logging.getLogger().setLevel(logging.DEBUG)
    wrong = "WRONG-GUESS-" + serve.new_token()
    engine = make_engine()
    async with Running(engine):
        bad = await connect(url(engine))
        await bad.send(json.dumps({"type": "engine_auth", "token": wrong}))
        await close_code(bad)
        assert engine.token not in (bad.close_reason or "") and wrong not in (
            bad.close_reason or ""
        )
        ws = await logged_in(engine)
        await call(ws, {"type": "ping"})
        await call(ws, {"type": "open_file", "path": "/nonexistent/file.nc"})
        await ws.close()
        second = await logged_in(engine)
        busy = await connect(url(engine))
        await busy.send(json.dumps({"type": "engine_auth", "token": engine.token}))
        text = await asyncio.wait_for(busy.recv(), 10)
        assert engine.token not in text
        await second.close()
    out = capsys.readouterr()
    for secret in (engine.token, wrong):
        assert secret not in caplog.text
        assert all(secret not in str(a) for r in caplog.records for a in (r.args or ()))
        assert secret not in out.out and secret not in out.err
    assert engine.token not in repr(engine)


# -- the account stays the user's own -------------------------------------------------------------


async def test_the_account_gate_still_applies_over_the_tunnel(gate_on):
    async with Running(make_engine()) as engine:
        ws = await logged_in(engine)
        refused = await call(ws, {"type": "open_file", "path": "/tmp/x.nc"})
        assert refused["type"] == "auth_required" and refused["reason"] == "no_session"
        ok = await call(
            ws, {"type": "auth_session", "access_token": gate_on.keys.token(), "reason": "start"}
        )
        assert ok["type"] == "auth_ok" and ok["email"] == "ada@example.test"
        assert (await call(ws, {"type": "list_modules"}))["type"] != "auth_required"
        bad = await call(ws, {"type": "auth_session", "access_token": "not.a.token"})
        assert bad["type"] == "auth_required"
        await ws.close()


async def test_a_token_that_expired_is_not_enough_for_a_remote_engine(gate_on):
    async with Running(make_engine()) as engine:
        ws = await logged_in(engine)
        old = gate_on.keys.token(iat=gate_on.now - 3 * 86_400)
        reply = await call(ws, {"type": "auth_session", "access_token": old})
        assert reply["type"] == "auth_required" and reply["reason"] == "expired"
        await ws.close()


async def test_the_app_never_overwrites_the_cluster_session_file(gate_on):
    core.login_with_password("ada@example.test", "correct horse 1")  # `rxd login` on the cluster
    store = SessionStore()
    before = store.path.read_text()
    async with Running(make_engine()) as engine:
        ws = await logged_in(engine)
        await call(
            ws,
            {
                "type": "auth_session",
                "access_token": gate_on.keys.token(),
                "device_id": "abcdef1234567890",
            },
        )
        assert (
            store.path.read_text() == before
        )  # not replaced by the app's token (no refresh token)
        assert not (store.directory / "device_id").exists()
        signed_out = await call(ws, {"type": "auth_signout"})
        assert signed_out["type"] == "auth_signed_out"
        assert store.path.exists()  # the app signing out does not sign the cluster account out
        await ws.close()
    assert core.require_account().email == "ada@example.test"


# -- idle and shutdown ----------------------------------------------------------------------------


class Timed:
    """Like :class:`Running`, but the test watches for the engine ending by itself."""

    def __init__(self, engine: serve.RemoteEngine) -> None:
        self.engine = engine
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> asyncio.Task:
        self.task = asyncio.create_task(self.engine.run())
        await asyncio.wait_for(self.engine.ready.wait(), 30)
        return self.task

    async def __aexit__(self, *exc) -> None:
        self.engine.stop.set()  # whatever happened, never leave the server behind
        assert self.task is not None
        await asyncio.wait_for(self.task, 15)


async def test_the_engine_stops_by_itself_when_the_app_does_nothing():
    engine = make_engine(idle_minutes=0.02)  # 1.2 s
    async with Timed(engine) as task:
        ws = await logged_in(engine)
        with contextlib.suppress(ConnectionClosed):  # the engine ends while we are still pinging
            for _ in range(10):  # pings and sign-in refreshes are not use
                await call(ws, {"type": "ping"})
                await asyncio.sleep(0.3)
        await asyncio.wait_for(task, 10)
        assert "idle" in engine.stop_reason
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)


async def test_real_requests_keep_the_engine_alive():
    engine = make_engine(idle_minutes=0.02)
    async with Timed(engine) as task:
        ws = await logged_in(engine)
        for _ in range(8):  # 2.4 s of list_modules: longer than the idle limit, but never idle
            assert (await call(ws, {"type": "list_modules"}))["type"] == "modules_list"
            await asyncio.sleep(0.3)
        assert not task.done()
        await ws.close()
        await asyncio.wait_for(task, 10)  # and once the requests stop it ends
        assert "idle" in engine.stop_reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_a_signal_shuts_the_engine_down_cleanly(sig):
    engine = make_engine()
    async with Timed(engine) as task:
        ws = await logged_in(engine)
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(task, 15)  # run() returned: no error, no hang
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 1001  # "going away": a clean close, not a dropped connection


# -- the whole command, as a process --------------------------------------------------------------


def _read_banner(proc: subprocess.Popen) -> str:
    assert proc.stdout is not None
    text = ""
    while "idle limit" not in text and "minutes without use" not in text:
        line = proc.stdout.readline()
        if not line:
            break
        text += line
    return text


async def test_rxd_serve_end_to_end(tmp_path):
    env = {
        **os.environ,
        "PYTHONPATH": str(PYTHON_DIR),
        # the project's own test setting: there is no account server to reach from a test
        "RADARX_AUTH_DISABLED": "1",
        "RADARX_CONFIG_DIR": str(tmp_path / "cfg"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "engine", "serve", "--idle-minutes", "5", "-v"],
        env=env,  # no cwd, close_fds=False: posix_spawn, not fork (see tests/account_helpers.py)
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=False,
    )
    try:
        banner = await asyncio.get_running_loop().run_in_executor(None, _read_banner, proc)
        fields = {
            ln.split()[0]: ln.split()[1]
            for ln in banner.splitlines()
            if ln.startswith(("  Address", "  Token"))
        }
        if not fields:
            proc.wait(timeout=20)
            raise AssertionError(f"no banner; exit {proc.returncode}: {proc.stderr.read()[-1500:]}")
        assert set(fields) == {"Address", "Token"}, banner
        address, token = fields["Address"], fields["Token"]
        host, port = address.split(":")
        assert host == "127.0.0.1" and 1024 <= int(port) <= 65535
        assert f"ssh -L {port}:localhost:{port} " in banner
        assert banner.count(token) == 1
        async with connect(f"ws://{address}") as ws:
            await ws.send(json.dumps({"type": "engine_auth", "token": token}))
            assert json.loads(await ws.recv())["type"] == "engine_auth_ok"
            assert (await call(ws, {"type": "ping"}))["type"] == "pong"
        async with connect(f"ws://{address}") as ws:
            await ws.send(json.dumps({"type": "engine_auth", "token": "x" * 43}))
            assert await close_code(ws) == 1008
        proc.send_signal(signal.SIGTERM)
        rest_out = proc.stdout.read() if proc.stdout else ""
        assert proc.wait(timeout=30) == 0  # a clean stop
        err = proc.stderr.read() if proc.stderr else ""
        assert token not in err and token not in rest_out  # not in the engine's log
        assert "Traceback" not in err
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
