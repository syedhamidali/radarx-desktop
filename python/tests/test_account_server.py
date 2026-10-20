"""The engine's WebSocket server and the account gate (docs/ACCOUNTS.md, docs/WEB.md).

A connection may do nothing but ping / get_web_info / auth_* until it presents a valid signed-in
account with `auth_session`. Mocked account server and a locally generated key; no real network.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

import server
from engine.account import core
from engine.account.store import SessionStore
from tests.account_helpers import KeyPair
from tests.web_helpers import WebClient, close_session, make_netcdf

DAY = 86_400.0


class Client:
    """A desktop-mode ConnectionHandler with a recording fake socket."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        ws = SimpleNamespace(remote_address=("127.0.0.1", 9))
        ws.send = self._send
        self.handler = server.ConnectionHandler(ws)  # type: ignore[arg-type]

    async def _send(self, raw) -> None:
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))

    async def call(self, msg: dict) -> list[dict]:
        start = len(self.sent)
        await self.handler.handle_message(json.dumps(msg))
        return self.sent[start:]


@pytest.fixture(autouse=True)
def _clean_server_state():
    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    server._last_file_id = None
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


def session_msg(fake, **kw) -> dict:
    return {
        "type": "auth_session",
        "access_token": fake.keys.token(iat=fake.now),
        "reason": "start",
        **kw,
    }


# Requests that would do real work. Each must be answered `auth_required`, nothing else.
REQUESTS = [
    {"type": "open_file", "path": "/tmp/anything.nc"},
    {"type": "list_modules"},
    {"type": "run_module", "module": "radarx_hid", "sweep": 0},
    {"type": "render", "variable": "DBZH", "sweep": 0},
    {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0},
    {"type": "export", "format": "netcdf"},
    {"type": "create_mosaic", "request_id": "m1"},
    {"type": "run_multidoppler", "request_id": "w1"},
    {"type": "qpe_info"},
    {"type": "list_recipes"},
    {"type": "list_nexrad_stations"},
    {"type": "save_text_file", "path": "x.txt", "content": "x"},
    {"type": "definitely_not_a_message"},
]


async def test_nothing_works_before_an_account_is_presented(gate_on, tmp_path):
    client = Client()
    path = make_netcdf(tmp_path / "scan.nc")
    for msg in [{"type": "open_file", "path": str(path)}, *REQUESTS]:
        replies = await client.call(msg)
        assert [r["type"] for r in replies] == ["auth_required"], (msg, replies)
        assert replies[0]["reason"] == "no_session"
        assert replies[0]["request_type"] == msg["type"]
        assert "not signed in" in replies[0]["message"]
    assert server._file_readers == {}  # the file was never opened: no processing happened


async def test_request_ids_are_echoed_so_the_ui_can_fail_the_right_request(gate_on):
    reply = (await Client().call({"type": "create_mosaic", "request_id": "m-7"}))[0]
    assert reply["request_id"] == "m-7"


async def test_ping_and_web_info_answer_without_an_account(gate_on):
    client = Client()
    assert (await client.call({"type": "ping"}))[0]["type"] == "pong"
    assert (await client.call({"type": "get_web_info"}))[0]["type"] == "web_info"


async def test_a_valid_session_opens_the_connection(gate_on, tmp_path):
    client = Client()
    replies = await client.call(session_msg(gate_on, device_id="abcdef1234567890"))
    assert replies[0]["type"] == "auth_ok"
    assert replies[0]["email"] == "ada@example.test" and replies[0]["stale"] is False
    assert (await client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
    path = make_netcdf(tmp_path / "scan.nc")
    types = [r["type"] for r in await client.call({"type": "open_file", "path": str(path)})]
    assert "file_opened" in types and "auth_required" not in types
    assert len(server._file_readers) == 1


async def test_the_session_is_shared_with_the_command_line_without_a_refresh_token(gate_on):
    await Client().call(session_msg(gate_on, device_id="abcdef1234567890"))
    store = SessionStore()
    data = json.loads(store.path.read_text())
    assert data["source"] == "app" and data["refresh_token"] is None
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert core.require_account().email == "ada@example.test"  # `rxd` and `import rxd` now work
    assert (store.directory / "device_id").read_text().strip() == "abcdef1234567890"


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda m, f: {**m, "access_token": m["access_token"][:-6] + "AAAAAA"}, "invalid"),
        (lambda m, f: {**m, "access_token": f.keys.token(aud="x")}, "invalid"),
        (
            lambda m, f: {**m, "access_token": f.keys.token(iss="https://evil.invalid/auth/v1")},
            "invalid",
        ),
        (lambda m, f: {**m, "access_token": KeyPair().token()}, "invalid"),
        (lambda m, f: {**m, "access_token": ""}, "invalid"),
        (lambda m, f: {**m, "access_token": 12345}, "invalid"),
        (lambda m, f: {k: v for k, v in m.items() if k != "access_token"}, "invalid"),
    ],
)
async def test_a_bad_token_is_refused_and_work_stays_blocked(gate_on, mutate, reason):
    client = Client()
    replies = await client.call(mutate(session_msg(gate_on), gate_on))
    assert replies[0]["type"] == "auth_required" and replies[0]["reason"] == reason
    assert (await client.call({"type": "list_modules"}))[0]["type"] == "auth_required"
    assert not SessionStore().path.exists()  # nothing was written for a token that did not verify


async def test_each_connection_must_present_its_own_account(gate_on):
    first, second = Client(), Client()
    await first.call(session_msg(gate_on))
    assert (await first.call({"type": "list_modules"}))[0]["type"] != "auth_required"
    assert (await second.call({"type": "list_modules"}))[0]["type"] == "auth_required"


async def test_an_expired_token_inside_the_offline_period_is_accepted_and_flagged(gate_on):
    client = Client()
    old = gate_on.keys.token(iat=gate_on.now - 5 * DAY)
    reply = (await client.call({"type": "auth_session", "access_token": old, "reason": "start"}))[0]
    assert reply["type"] == "auth_ok" and reply["stale"] is True and reply["grace_days_left"] == 25
    assert (await client.call({"type": "list_modules"}))[0]["type"] != "auth_required"


async def test_an_expired_token_after_the_offline_period_is_refused(gate_on):
    client = Client()
    old = gate_on.keys.token(iat=gate_on.now - 31 * DAY)
    reply = (await client.call({"type": "auth_session", "access_token": old}))[0]
    assert reply["type"] == "auth_required" and reply["reason"] == "offline_expired"
    assert "30 days" in reply["message"]


async def test_a_connection_that_outlives_the_offline_period_is_refused_again(gate_on):
    client = Client()
    await client.call(session_msg(gate_on))
    assert (await client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
    gate_on.now += 31 * DAY
    reply = (await client.call({"type": "list_modules"}))[0]
    assert reply["type"] == "auth_required" and reply["reason"] == "offline_expired"


async def test_signing_out_blocks_the_connection_and_clears_the_shared_file(gate_on):
    client = Client()
    await client.call(session_msg(gate_on))
    assert SessionStore().path.exists()
    assert (await client.call({"type": "auth_signout"}))[0]["type"] == "auth_signed_out"
    assert not SessionStore().path.exists()
    assert (await client.call({"type": "list_modules"}))[0]["type"] == "auth_required"
    assert (await client.call({"type": "auth_signout"}))[0][
        "type"
    ] == "auth_signed_out"  # idempotent


async def test_auth_status_reports_the_connection(gate_on):
    client = Client()
    assert (await client.call({"type": "auth_status"}))[0]["type"] == "auth_required"
    await client.call(session_msg(gate_on))
    assert (await client.call({"type": "auth_status"}))[0]["type"] == "auth_ok"


async def test_the_engine_session_token_is_still_checked_first(gate_on, monkeypatch):
    monkeypatch.setattr(server, "EXPECTED_SESSION_TOKEN", "secret-token")
    client = Client()
    reply = (await client.call(session_msg(gate_on)))[0]
    assert reply["type"] == "error" and "Unauthorized" in reply["message"]
    reply = (await client.call({**session_msg(gate_on), "token": "secret-token"}))[0]
    assert reply["type"] == "auth_ok"


async def test_development_bypass_opens_everything_but_not_in_an_installer(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_AUTH_DISABLED", "1")
    client = Client()
    assert (await client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
    reply = (await client.call(session_msg(gate_on)))[0]
    assert reply["type"] == "auth_ok" and reply["bypass"] is True
    # the same variable in a frozen (installer) engine changes nothing
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = Client()
    assert (await frozen.call({"type": "list_modules"}))[0]["type"] == "auth_required"


# -- the public web service --------------------------------------------------------------------


async def test_web_mode_has_the_same_gate_on_by_default(gate_on, tmp_path):
    web_client = WebClient()
    try:
        for msg in (
            {"type": "list_modules"},
            {"type": "upload_start", "upload_id": "u", "filename": "a.nc", "size": 5},
        ):
            reply = (await web_client.call(msg))[0]
            assert reply["type"] == "auth_required", msg
        reply = (await web_client.call(session_msg(gate_on)))[0]
        assert reply["type"] == "auth_ok"
        assert (await web_client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
        assert (
            not SessionStore().path.exists()
        )  # many users share a web engine: nothing is persisted
    finally:
        close_session(web_client.ctx)


async def test_web_operator_can_turn_the_gate_off_but_an_installer_cannot(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_WEB_REQUIRE_ACCOUNT", "0")
    web_client = WebClient()
    try:
        assert (await web_client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert (await web_client.call({"type": "list_modules"}))[0]["type"] == "auth_required"
    finally:
        close_session(web_client.ctx)


async def test_web_connections_do_not_share_accounts(gate_on):
    a, b = WebClient(), WebClient()
    try:
        await a.call(session_msg(gate_on))
        assert (await a.call({"type": "list_modules"}))[0]["type"] != "auth_required"
        assert (await b.call({"type": "list_modules"}))[0]["type"] == "auth_required"
    finally:
        close_session(a.ctx)
        close_session(b.ctx)


async def test_over_a_real_websocket(gate_on):
    """End to end through the real server loop: refused, then accepted after auth_session."""
    import websockets

    async with websockets.serve(server._handle_connection, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "list_modules"}))
            assert json.loads(await ws.recv())["type"] == "auth_required"
            await ws.send(json.dumps({"type": "ping"}))
            assert json.loads(await ws.recv())["type"] == "pong"
            await ws.send(json.dumps(session_msg(gate_on)))
            assert json.loads(await ws.recv())["type"] == "auth_ok"
            await ws.send(json.dumps({"type": "list_modules"}))
            assert json.loads(await ws.recv())["type"] != "auth_required"
