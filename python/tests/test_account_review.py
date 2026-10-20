"""Regression tests for the independent review of the account enforcement (docs/ACCOUNTS.md).

Each test starts from a reproduction the reviewer made (and I re-ran before fixing): it failed
before the fix and passes now. Mocked account server, local test key, no real network.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time

import pytest

import server
from engine import cli
from engine.account import activity, config, core, gotrue, http, tokens
from engine.account.errors import AccountRequired, AccountServerError
from engine.account.store import SessionStore, StoredSession, device_id, set_device_id
from tests.account_helpers import REAL_REQUEST_JSON, Blackhole, KeyPair
from tests.test_account_server import Client, session_msg
from tests.web_helpers import WebClient, close_session

DAY = 86_400.0
UID = "11111111-2222-3333-4444-555555555555"


def sign_in_cli(fake):
    return core.login_with_password("ada@example.test", "correct horse 1")


def queued():
    path = SessionStore().directory / "activity-queue.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# -- 3a: the web service needs a token that has not expired --------------------------------------


def test_web_mode_refuses_a_token_that_expired_long_ago(gate_on):
    old = gate_on.keys.token(iat=gate_on.now - 29 * DAY)  # review F1: this was accepted
    with pytest.raises(AccountRequired) as err:
        core.verify_presented_token(old, persist=False)
    assert err.value.reason == "expired"
    # a token inside its hour is fine; the desktop engine (offline allowance) still accepts old ones
    assert core.verify_presented_token(gate_on.keys.token(), persist=False).stale is False
    assert core.verify_presented_token(old, persist=True).stale is True


async def test_a_web_connection_lapses_when_its_token_expires(gate_on):
    web_client = WebClient()
    try:
        await web_client.call(session_msg(gate_on))
        assert (await web_client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
        gate_on.now += 2 * 3600  # an hour and more later, nothing renewed
        reply = (await web_client.call({"type": "list_modules"}))[0]
        assert reply["type"] == "auth_required" and reply["reason"] == "expired"
        # the browser renews the token (Supabase) and presents it again
        reply = (await web_client.call(session_msg(gate_on)))[0]
        assert reply["type"] == "auth_ok"
        assert (await web_client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
    finally:
        close_session(web_client.ctx)


# -- 6: a wrong clock reading must not lock the user out for good --------------------------------


def test_a_fresh_token_clears_a_clock_reading_that_ran_into_the_future(gate_on):
    real = gate_on.now
    core.verify_presented_token(gate_on.keys.token(iat=real), persist=True)
    gate_on.now = real + 400 * DAY  # the clock was briefly wrong (VM resume, dead RTC battery)
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()
    assert SessionStore().load().high_water >= real + 399 * DAY
    gate_on.now = real + 1  # the clock is right again; the app presents a fresh token
    account = core.verify_presented_token(gate_on.keys.token(iat=gate_on.now), persist=True)
    assert account.stale is False  # review F2: this was refused as offline_expired
    assert SessionStore().load().high_water <= real + 2
    core.reset_runtime_state()
    assert core.require_account().email == "ada@example.test"


def test_an_old_token_cannot_be_used_to_reset_the_clock_mark(gate_on):
    """The reset needs a token both fresh by the server's clock and newer than the one on file."""
    real = gate_on.now
    first = gate_on.keys.token(iat=real)
    core.verify_presented_token(first, persist=True)
    gate_on.now = real + 40 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()  # offline_expired: records the reading
    gate_on.now = real + 10 * 60  # the user sets the clock back to just after the token was issued
    with pytest.raises(AccountRequired):
        core.verify_presented_token(first, persist=True)  # the same old token: no reset


# -- 7: refresh error handling -------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 404, 407, 408, 429, 500, 502, 503])
def test_a_proxy_or_outage_answer_on_refresh_keeps_the_session(gate_on, monkeypatch, status):
    sign_in_cli(gate_on)
    gate_on.now += 2 * 3600
    core.reset_runtime_state()

    def proxy(refresh_token, timeout=5.0, late=None):
        raise AccountServerError(status, "", "Forbidden by corporate proxy")

    monkeypatch.setattr(gotrue, "refresh", proxy)
    account = core.require_account()  # review F6: this deleted the file (and the refresh token)
    assert account.stale is True  # offline allowance
    assert SessionStore().path.exists() and SessionStore().load().refresh_token


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_grant"),
        (400, "refresh_token_not_found"),
        (400, "refresh_token_already_used"),
        (401, "session_not_found"),
        (403, "user_banned"),
    ],
)
def test_only_the_servers_own_refusal_ends_the_session(gate_on, monkeypatch, status, code):
    sign_in_cli(gate_on)
    gate_on.now += 2 * 3600
    core.reset_runtime_state()

    def refused(refresh_token, timeout=5.0, late=None):
        raise AccountServerError(status, code, "gone")

    monkeypatch.setattr(gotrue, "refresh", refused)
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "revoked" and not SessionStore().path.exists()


def test_rotated_tokens_are_saved_before_they_are_checked(gate_on, monkeypatch):
    """Review F7: the old refresh token is spent by the refresh; if the new access token then failed
    verification, the spent token was what stayed on disk."""
    sign_in_cli(gate_on)
    gate_on.now += 2 * 3600
    core.reset_runtime_state()
    old_refresh = SessionStore().load().refresh_token
    other = KeyPair(kid="rotated-key")
    real = gotrue.refresh

    def rotated(refresh_token, timeout=5.0, late=None):
        session = real(refresh_token, timeout=timeout)
        session["access_token"] = other.token(iat=gate_on.now)  # signed with a key we cannot find
        return session

    monkeypatch.setattr(gotrue, "refresh", rotated)
    monkeypatch.setattr(tokens, "JWKS_REFETCH_MIN_S", 10**9)
    tokens.current_jwks(refetch=True, now=gate_on.now)
    with pytest.raises(AccountRequired):
        core.require_account()
    kept = SessionStore().load().refresh_token
    assert kept != old_refresh and kept.startswith("refresh-")  # the NEW refresh token is on disk


def test_tls_trusts_the_system_store_plus_certifi(monkeypatch):
    calls = {}

    class Ctx:
        def load_verify_locations(self, cafile=None, **kw):
            calls["cafile"] = cafile

    monkeypatch.setattr(http.ssl, "create_default_context", lambda *a, **k: Ctx())
    http._context()
    import certifi

    assert calls["cafile"] == certifi.where()


# -- 5: no network step may outlast its deadline --------------------------------------------------


@pytest.fixture()
def blackhole(gate_on, monkeypatch):
    """The account server accepts connections and never answers; the real HTTP code is used."""
    hole = Blackhole()
    monkeypatch.setenv("RADARX_SUPABASE_URL", hole.url)
    monkeypatch.setattr(http, "request_json", REAL_REQUEST_JSON)
    kp = KeyPair()
    tokens.reset_cache()
    tokens._memory.update(url=config.jwks_url(), jwks=kp.jwks, fetched_at=0.0, tried_at=time.time())
    gate_on.hole_keys = kp
    gate_on.hole_issuer = f"{hole.url}/auth/v1"
    yield hole
    hole.close()


def store_app_session(fake, *, age=0.0):
    token = fake.hole_keys.token(iss=fake.hole_issuer, iat=time.time() - age)
    SessionStore().save(
        StoredSession(access_token=token, user_id=UID, email="ada@example.test", source="app",
                      issued_at=time.time() - age, high_water=time.time())
    )  # fmt: skip
    core.reset_runtime_state()


def test_flush_is_bounded_when_the_server_never_answers(gate_on, blackhole):
    store_app_session(gate_on)
    core.require_account()
    activity.record("command", surface="cli", command="info")
    started = time.monotonic()
    sent = activity.flush(timeout=0.5)
    took = time.monotonic() - started
    assert sent == 0 and took < 2.0, took  # review: flush(0.5) took 8.0 s
    assert len(queued()) == 1  # kept for later


def test_a_command_is_not_held_back_by_a_hung_server(gate_on, blackhole, capsys):
    store_app_session(gate_on)
    started = time.monotonic()
    assert cli.main(["modules", "--json"]) == 0
    took = time.monotonic() - started
    capsys.readouterr()
    assert took < 6.0, took  # review: rxd modules took 8.9 s


def test_the_expiry_probe_is_bounded_too(gate_on, blackhole):
    store_app_session(gate_on, age=2 * 3600)  # expired: the check probes the server
    started = time.monotonic()
    account = core.require_account()  # unreachable (hung): the offline allowance applies
    assert account.stale is True
    assert time.monotonic() - started < 4.0


def test_request_json_has_a_total_deadline(gate_on, blackhole, monkeypatch):
    monkeypatch.setattr(http, "request_json", REAL_REQUEST_JSON)
    started = time.monotonic()
    with pytest.raises(AccountServerError) as err:
        http.request_json("GET", f"{blackhole.url}/x", timeout=0.4)
    assert err.value.status == 0 and time.monotonic() - started < 1.5


# -- 9: a web service with the gate off ------------------------------------------------------------


async def test_web_gate_off_answers_auth_session_instead_of_failing(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_WEB_REQUIRE_ACCOUNT", "0")
    web_client = WebClient()
    try:
        replies = await web_client.call({"type": "auth_session", "access_token": "x.y.z"})
        assert [r["type"] for r in replies] == ["auth_ok"] and replies[0]["bypass"] is True
        assert (await web_client.call({"type": "list_modules"}))[0]["type"] != "auth_required"
    finally:
        close_session(web_client.ctx)


# -- 12: smaller items -----------------------------------------------------------------------------


def test_device_ids_are_ascii_letters_and_digits_only(tmp_path):
    set_device_id(
        "é" * 10, tmp_path
    )  # review F4: str.isalnum() accepts these; the database does not
    assert not (tmp_path / "device_id").exists()
    (tmp_path / "device_id").write_text("é" * 10)
    fresh = device_id(tmp_path)
    assert re.fullmatch(r"[A-Za-z0-9]{8,80}", fresh) and fresh != "é" * 10
    set_device_id("abcdef1234567890", tmp_path)
    assert device_id(tmp_path) == "abcdef1234567890"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://x.supabase.co/auth/v1", True),
        ("http://127.0.0.1:5000/x", True),
        ("http://localhost:5000/x", True),
        ("http://[::1]:5000/x", True),
        ("http://127.0.0.1.evil.example/x", False),
        ("http://localhost.evil.example/x", False),
        ("http://127.0.0.1@evil.example/x", False),
        ("http://localhost@evil.example/x", False),
        ("http://evil.example/x", False),
        ("ftp://127.0.0.1/x", False),
        ("https:///nohost", False),
        ("//127.0.0.1/x", False),
    ],
)
def test_the_http_exception_parses_the_host(url, ok):
    assert http.url_allowed(url) is ok


def test_the_session_folder_is_only_made_private_when_radarx_made_it(
    gate_on, tmp_path, monkeypatch
):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    monkeypatch.setenv("RADARX_CONFIG_DIR", str(shared))
    sign_in_cli(gate_on)
    assert stat.S_IMODE(os.stat(shared).st_mode) == 0o755  # a folder the user chose is left alone
    assert stat.S_IMODE(os.stat(shared / "session.json").st_mode) == 0o600
    fresh = tmp_path / "made-by-radarx" / "inner"
    monkeypatch.setenv("RADARX_CONFIG_DIR", str(fresh))
    sign_in_cli(gate_on)
    assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o700


def test_a_session_file_that_cannot_be_read_is_not_reported_as_signed_out(gate_on):
    sign_in_cli(gate_on)
    path = SessionStore().path
    os.chmod(path, 0o000)
    try:
        core.reset_runtime_state()
        with pytest.raises(AccountRequired) as err:
            core.require_account()
        assert err.value.reason == "unavailable" and str(path) in str(err.value)
        assert "permissions" in str(err.value)
        assert SessionStore().load() is None  # the never-raising reader is unchanged
    finally:
        os.chmod(path, 0o600)
    core.reset_runtime_state()
    assert core.require_account().email == "ada@example.test"


def test_replace_retries_a_busy_file_on_windows(tmp_path, monkeypatch):
    from engine.account import store

    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return os.rename(src, dst)

    monkeypatch.setattr(store.os, "name", "nt")
    monkeypatch.setattr(store.os, "replace", flaky)
    store.write_private(tmp_path / "f.json", "{}")
    assert len(calls) == 3 and (tmp_path / "f.json").read_text() == "{}"


# -- 12: activity: attribution, vocabulary, per-visitor cap, 401, bisecting ------------------------


def test_events_are_attributed_to_the_connections_account_only(gate_on):
    sign_in_cli(gate_on)
    activity.record(
        "export", uid="somebody-else", format="csv"
    )  # the file now belongs to another user
    assert queued() == []
    activity.record("export", uid=UID, format="csv")
    assert [r["detail"] for r in queued()] == [{"format": "csv"}]


async def test_a_connection_does_not_log_under_an_account_that_replaced_it(gate_on, tmp_path):
    client = Client()
    await client.call(session_msg(gate_on))  # ada's connection
    other = gate_on.keys.token(sub="99999999-0000-0000-0000-000000000000", iat=gate_on.now)
    core.verify_presented_token(other, persist=True)  # another app window / rxd login took the file
    client.handler._record("export", format="csv")
    await _drain()
    assert [r for r in queued() if r["event"] == "export"] == []


async def _drain():
    import asyncio

    await asyncio.get_running_loop().run_in_executor(server._activity_pool(), lambda: None)


async def test_app_signout_clears_the_shared_file_even_if_never_authorised(gate_on):
    sign_in_cli(gate_on)
    client = Client()  # a connection that never presented an account
    assert (await client.call({"type": "auth_signout"}))[0]["type"] == "auth_signed_out"
    assert not SessionStore().path.exists()


def test_the_vocabulary_is_fixed(gate_on):
    detail = activity.clean_detail
    assert detail("error", {"class": "AcmeClientStormError"}) == {"class": "other"}
    assert detail("error", {"class": "XrdInputError"}) == {"class": "XrdInputError"}
    assert detail("command", {"command": "acme_secret_cmd"}) == {"command": "other"}
    assert detail("command", {"command": "run"}) == {"command": "run"}
    assert detail("export", {"format": "weird_user_format"}) == {"format": "other"}
    assert detail("mosaic_run", {"type": "my project name"}) == {"type": "other"}
    assert detail("module_run", {"module": "acme_client_project"}) == {"module": "custom"}
    assert detail("module_run", {"module": "radarx_hid"}) == {"module": "radarx_hid"}
    assert detail("file_opened", {"format": "/a/b.nc"}) == {}  # not even a plain word: dropped
    assert set(cli.COMMANDS) == activity.KNOWN_COMMANDS


def test_a_plugin_error_class_is_recorded_as_other(gate_on):
    sign_in_cli(gate_on)

    class AcmeClientStormError(Exception):
        pass

    activity.record_error(AcmeClientStormError("x"), surface="api")
    assert [r["detail"] for r in queued()] == [{"class": "other"}]


def test_the_hourly_cap_is_per_visitor(gate_on):
    for _ in range(activity.MAX_EVENTS_PER_HOUR + 20):
        activity.record("command", token="tok-a", device="visitorAAAA1111", command="info")
    activity.record("command", token="tok-b", device="visitorBBBB2222", command="info")
    counts = {}
    for item in activity._memory_only:
        counts[item["row"]["device_id"]] = counts.get(item["row"]["device_id"], 0) + 1
    assert counts["visitorBBBB2222"] == 1  # one noisy visitor does not silence the others


def test_a_web_visitor_token_that_the_server_refuses_is_dropped_not_kept_forever(gate_on):
    stale = gate_on.keys.token(iat=time.time() - 3 * 3600)  # expired 2 hours ago
    activity.record("command", token=stale, device="visitorAAAA1111", command="info")
    gate_on.activity_mode = "unauthorized"
    assert activity.flush(2.0) == 0
    assert activity._memory_only == []  # nobody can ever present a fresher token for this buffer
    live = gate_on.keys.token(iat=time.time())
    activity.record("command", token=live, device="visitorAAAA1111", command="info")
    assert activity.flush(2.0) == 0
    assert len(activity._memory_only) == 1  # a token that is still current: try again later


def test_one_bad_row_does_not_cost_the_whole_batch(gate_on):
    sign_in_cli(gate_on)
    for name in ("info", "run", "export", "plot", "cells", "check", "winds"):
        activity.record("command", surface="cli", command=name)
    gate_on.reject_row = lambda row: row["detail"].get("command") == "export"
    sent = activity.flush(5.0)
    assert sent == 6
    assert sorted(r["detail"]["command"] for r in gate_on.activity_rows) == sorted(
        ["info", "run", "plot", "cells", "check", "winds"]
    )
    assert queued() == []  # the bad row is gone, nothing is retried forever
    assert gate_on.activity_posts <= 1 + 2 * 3  # bisecting, not one request per row


# -- 11: the test-suite hygiene itself -----------------------------------------------------------


def test_stop_sender_and_the_recorder_pool_leave_no_threads(gate_on):
    sign_in_cli(gate_on)
    activity.set_background(True)
    activity.record("command", surface="cli", command="info")
    assert any(t.name == "radarx-activity" for t in threading.enumerate())
    activity.stop_sender()
    assert not any(t.name == "radarx-activity" for t in threading.enumerate())
    server._activity_pool().submit(lambda: None).result()
    assert any(t.name.startswith("radarx-rec") for t in threading.enumerate())
    server.shutdown_activity_pool()
    assert not any(t.name.startswith("radarx-rec") for t in threading.enumerate())


SECRET_SHAPES = [
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),  # a JWT
    re.compile(r"refresh-\d+"),
    re.compile(r"sb_(secret|publishable)_[A-Za-z0-9_-]+"),
]


def assert_no_secrets(text: str, *literals: str) -> None:
    for shape in SECRET_SHAPES:
        assert not shape.search(text), f"a secret-looking value in: {text[:120]!r}"
    for literal in literals:
        assert literal not in text, "a known secret in the text"


def test_the_secret_checker_catches_a_leak():
    """A check that cannot fail proves nothing: feed it text that does leak."""
    token = KeyPair().token()
    with pytest.raises(AssertionError):
        assert_no_secrets(f"could not verify {token}")
    with pytest.raises(AssertionError):
        assert_no_secrets("refresh-12 was rejected")
    with pytest.raises(AssertionError):
        assert_no_secrets("the password hunter2x failed", "hunter2x")


def test_errors_and_logs_never_contain_tokens_or_passwords(gate_on, capsys, caplog):
    sign_in_cli(gate_on)
    stored = SessionStore().load()
    secrets = [stored.access_token, stored.refresh_token, "correct horse 1"]
    texts: list[str] = []
    forged = stored.access_token[:-4] + "AAAA"
    with pytest.raises(tokens.TokenInvalid) as err:
        tokens.verify_access_token(forged, now=gate_on.now)
    texts += [str(err.value), repr(err.value)]
    for status, code in ((400, "invalid_grant"), (0, "unreachable"), (503, "")):
        texts.append(str(AccountServerError(status, code, "Invalid Refresh Token")))
    gate_on.now += 40 * DAY
    gate_on.mode = "unreachable"
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as gate_err:
        core.require_account()
    texts += [str(gate_err.value), repr(gate_err.value)]
    assert cli.main(["modules"]) == 5
    out = capsys.readouterr()
    texts += [out.out, out.err, caplog.text]
    for text in texts:
        assert_no_secrets(text, *secrets)


def test_the_version_sent_is_never_free_text(monkeypatch):
    cases = [
        ("0.3.0", "0.3.0"),
        ("0.3.0-rc1", "0.3.0-rc1"),
        ("Ada Lovelace", "unknown"),
        ("/Users/ada/app", "unknown"),
        ("", "unknown"),
    ]
    for value, expected in cases:
        monkeypatch.setenv("XRADAR_DESKTOP_VERSION", value)
        if value == "":
            monkeypatch.setattr("importlib.metadata.version", lambda name: "weird version string")
        assert activity.app_version() == expected, value


def test_the_sql_and_the_client_agree_on_the_version_and_os_shape():
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[2] / "supabase/migrations/20261021000000_activity.sql"
    ).read_text()
    assert "'macOS', 'Windows', 'Linux', 'Web', 'Other'" in sql
    assert activity._os_family() in {"macOS", "Windows", "Linux", "Other"}
