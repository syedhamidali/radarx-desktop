"""The account gate: token verification, the session file, the offline rule, the development bypass.

Everything runs against a locally generated test key and a mocked account server (tests/conftest.py
``gate_on``); no real network, no real account. The rules are the ones in docs/ACCOUNTS.md.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading

import jwt
import pytest

from engine.account import config, core, tokens
from engine.account.errors import AccountRequired
from engine.account.store import SessionStore, StoredSession
from tests.account_helpers import TEST_ISS, KeyPair

DAY = 86_400.0


def sign_in(fake, *, via="cli"):
    """Store a session the way `rxd login` (cli) or the app's engine hand-over (app) does."""
    if via == "cli":
        return core.login_with_password("ada@example.test", "correct horse 1")
    account = core.verify_presented_token(fake.keys.token(iat=fake.now), persist=True)
    return account


# -- the bypass ---------------------------------------------------------------------------------


def test_no_session_is_refused(gate_on):
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "no_session"
    assert "not signed in" in str(err.value)


def test_import_never_needs_an_account(gate_on):
    import importlib

    import rxd

    importlib.reload(rxd)  # importing is free; only using it is gated
    assert callable(rxd.open)


def test_bypass_works_from_source_and_is_ignored_when_frozen(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_AUTH_DISABLED", "1")
    assert config.auth_disabled() is True
    assert core.require_account().bypassed
    # an installer build (PyInstaller sets sys.frozen) ignores the variable completely
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.auth_disabled() is False
    with pytest.raises(AccountRequired):
        core.require_account()


@pytest.mark.parametrize("value", ["0", "true", "yes", "", "2"])
def test_only_exactly_1_switches_the_gate_off(gate_on, monkeypatch, value):
    monkeypatch.setenv("RADARX_AUTH_DISABLED", value)
    assert config.auth_disabled() is False


def test_frozen_build_ignores_project_overrides(gate_on, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.project_url() == config.DEFAULT_PROJECT_URL
    assert config.publishable_key() == config.DEFAULT_PUBLISHABLE_KEY
    monkeypatch.setenv("RADARX_WEB_REQUIRE_ACCOUNT", "0")
    assert config.web_gate_enabled() is True


def test_web_gate_default_on_and_operator_can_turn_it_off(gate_on, monkeypatch):
    assert config.web_gate_enabled() is True
    monkeypatch.setenv("RADARX_WEB_REQUIRE_ACCOUNT", "0")
    assert config.web_gate_enabled() is False


# -- tokens -------------------------------------------------------------------------------------


def test_valid_token_is_accepted(gate_on):
    claims = tokens.verify_access_token(gate_on.keys.token(), now=gate_on.now)
    assert claims.email == "ada@example.test"
    assert claims.expired(gate_on.now) is False


def test_tampered_payload_is_rejected(gate_on):
    token = gate_on.keys.token()
    head, body, sig = token.split(".")
    other = gate_on.keys.token(email="mallory@example.test").split(".")[1]
    for forged in (f"{head}.{other}.{sig}", f"{head}.{body}.{sig[:-3]}AAA", f"{head}.{body}."):
        with pytest.raises(tokens.TokenInvalid):
            tokens.verify_access_token(forged, now=gate_on.now)


def test_token_signed_by_another_key_is_rejected(gate_on):
    stranger = KeyPair()  # same kid, different key: a forged token
    with pytest.raises(tokens.TokenInvalid) as err:
        tokens.verify_access_token(stranger.token(), now=gate_on.now)
    assert err.value.detail == "signature"


def test_wrong_audience_and_wrong_issuer_are_rejected(gate_on):
    with pytest.raises(tokens.TokenInvalid) as err:
        tokens.verify_access_token(gate_on.keys.token(aud="somebody-else"), now=gate_on.now)
    assert err.value.detail == "audience"
    with pytest.raises(tokens.TokenInvalid) as err:
        tokens.verify_access_token(
            gate_on.keys.token(iss="https://evil.example.invalid/auth/v1"), now=gate_on.now
        )
    assert err.value.detail == "issuer"


def test_missing_claims_anonymous_and_non_user_roles_are_rejected(gate_on):
    for kwargs in (
        {"drop": ("exp",)},
        {"drop": ("sub",)},
        {"drop": ("iat",)},
        {"role": "anon"},
        {"extra": {"is_anonymous": True}},
    ):
        with pytest.raises(tokens.TokenInvalid):
            tokens.verify_access_token(gate_on.keys.token(**kwargs), now=gate_on.now)


def test_algorithm_none_and_hs256_confusion_are_rejected(gate_on):
    claims = {
        "iss": TEST_ISS,
        "aud": "authenticated",
        "sub": "u",
        "role": "authenticated",
        "iat": 1,
        "exp": 4_000_000_000,
    }
    unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"kid": gate_on.keys.kid})
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify_access_token(unsigned, now=gate_on.now)
    # the classic attack: sign with HS256 using the PUBLIC key as the secret
    public_pem = gate_on.keys.private.public_key().public_bytes(
        __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
        __import__(
            "cryptography"
        ).hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    try:
        forged = jwt.encode(
            claims, public_pem, algorithm="HS256", headers={"kid": gate_on.keys.kid}
        )
    except Exception:  # newer PyJWT refuses to even make it
        forged = None
    if forged:
        with pytest.raises(tokens.TokenInvalid) as err:
            tokens.verify_access_token(forged, now=gate_on.now)
        assert err.value.detail == "algorithm"


def test_garbage_is_rejected_without_crashing(gate_on):
    for junk in ("", "abc", "a.b.c", "a.b", "." * 2, "x" * 20000, None, 5, b"bytes"):
        with pytest.raises(tokens.TokenInvalid):
            tokens.verify_access_token(junk, now=gate_on.now)


def test_unknown_key_triggers_one_fetch_and_is_cached_on_disk(gate_on):
    gate_on.calls.clear()
    tokens.verify_access_token(gate_on.keys.token(), now=gate_on.now)
    assert sum(1 for _, path in gate_on.calls if path.endswith("jwks.json")) == 1
    tokens.verify_access_token(gate_on.keys.token(), now=gate_on.now)
    assert (
        sum(1 for _, path in gate_on.calls if path.endswith("jwks.json")) == 1
    )  # cached in memory
    tokens.reset_cache()
    gate_on.calls.clear()
    tokens.verify_access_token(
        gate_on.keys.token(), now=gate_on.now
    )  # a new process: from the disk cache
    assert not any(path.endswith("jwks.json") for _, path in gate_on.calls)
    cache = json.loads((tokens.config_dir() / "jwks.json").read_text())
    assert cache["jwks"]["keys"][0]["kid"] == gate_on.keys.kid
    assert "d" not in cache["jwks"]["keys"][0]  # a public key only


def test_key_rotation_fetches_the_new_document_but_not_more_than_every_five_minutes(gate_on):
    tokens.verify_access_token(gate_on.keys.token(), now=gate_on.now)
    new = KeyPair(kid="rotated-2")
    gate_on.keys = new  # the project rotated its key
    gate_on.calls.clear()
    tokens.verify_access_token(new.token(), now=gate_on.now + 400)
    assert any(path.endswith("jwks.json") for _, path in gate_on.calls)
    stranger = KeyPair(kid="never-published")
    gate_on.calls.clear()
    for _ in range(3):
        with pytest.raises(tokens.TokenInvalid):
            tokens.verify_access_token(stranger.token(), now=gate_on.now + 401)
    assert sum(1 for _, path in gate_on.calls if path.endswith("jwks.json")) <= 1


def test_built_in_public_key_works_with_no_network(gate_on, monkeypatch):
    """The project's real public key ships in the code, so a first check needs no download."""
    from engine.account.jwks_seed import SEED_JWKS, SEED_URL

    monkeypatch.setenv("RADARX_SUPABASE_URL", SEED_URL.split("/auth/v1")[0])
    tokens.reset_cache()
    doc = tokens.current_jwks()
    assert doc == SEED_JWKS
    key = doc["keys"][0]
    assert key["alg"] == "ES256" and key["kty"] == "EC" and "d" not in key


# -- the stored session: CLI-owned (refresh token) and app-owned (access token only) --------------


def test_login_stores_a_private_session_file(gate_on):
    account = sign_in(gate_on)
    assert account.email == "ada@example.test"
    path = SessionStore().path
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)
    data = json.loads(path.read_text())
    assert data["source"] == "cli" and data["refresh_token"].startswith("refresh-")
    assert "password" not in path.read_text().lower()
    assert core.require_account().email == "ada@example.test"


def test_login_with_wrong_password_stores_nothing(gate_on):
    from engine.account.errors import AccountServerError

    with pytest.raises(AccountServerError):
        core.login_with_password("ada@example.test", "wrong")
    assert not SessionStore().path.exists()


def test_login_refuses_a_token_that_does_not_verify(gate_on, monkeypatch):
    other = KeyPair()
    monkeypatch.setattr(
        gate_on,
        "_session",
        lambda email: {"access_token": other.token(), "refresh_token": "refresh-x"},
    )
    with pytest.raises(AccountRequired):
        core.login_with_password("ada@example.test", "correct horse 1")
    assert not SessionStore().path.exists()


def test_fresh_token_needs_no_network(gate_on):
    sign_in(gate_on)
    gate_on.calls.clear()
    gate_on.mode = "unreachable"
    gate_on.now += 600
    core.reset_runtime_state()
    assert core.require_account().email == "ada@example.test"
    assert gate_on.calls == []


def test_expired_token_is_refreshed_online_and_saved(gate_on):
    sign_in(gate_on)
    old = SessionStore().load()
    gate_on.now += 2 * 3600  # the access token (1 h) has expired
    core.reset_runtime_state()
    account = core.require_account()
    assert account.stale is False
    new = SessionStore().load()
    assert new.access_token != old.access_token and new.refresh_token != old.refresh_token
    assert any("grant_type=refresh_token" in path for _, path in gate_on.calls)


def test_refresh_just_before_expiry_keeps_a_long_command_alive(gate_on):
    sign_in(gate_on)
    gate_on.now += 3600 - 30  # 30 s left
    core.reset_runtime_state()
    before = len([c for c in gate_on.calls if "refresh_token" in c[1]])
    core.require_account()
    assert len([c for c in gate_on.calls if "refresh_token" in c[1]]) == before + 1


def test_refresh_refused_by_the_server_ends_the_session(gate_on):
    sign_in(gate_on)
    gate_on.now += 2 * 3600
    gate_on.mode = "rejected"
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "revoked"
    assert not SessionStore().path.exists()  # the useless file is gone


def test_offline_inside_30_days_is_accepted(gate_on):
    sign_in(gate_on)
    gate_on.mode = "unreachable"
    start = SessionStore().load().issued_at
    for days in (1, 15, 29, 29.9):
        gate_on.now = start + days * DAY
        core.reset_runtime_state()
        account = core.require_account()
        assert account.stale is True and account.grace_days_left is not None


def test_offline_after_30_days_is_refused(gate_on):
    sign_in(gate_on)
    gate_on.mode = "unreachable"
    start = SessionStore().load().issued_at
    gate_on.now = start + 30.5 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "offline_expired"
    assert "30 days" in str(err.value)


def test_a_clock_set_backwards_cannot_extend_the_offline_period(gate_on):
    sign_in(gate_on)
    gate_on.mode = "unreachable"
    start = SessionStore().load().issued_at
    gate_on.now = start + 31 * DAY  # past the limit: refused, and the reading is remembered
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()
    gate_on.now = start + 2 * DAY  # the user sets the clock back
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "offline_expired"
    # ... but going online again fixes it (the server vouches for the account anew)
    gate_on.mode = "ok"
    core.reset_runtime_state()
    assert core.require_account().stale is False


def test_clock_rolled_back_does_not_revive_an_expired_token_either(gate_on):
    sign_in(gate_on)
    gate_on.mode = "unreachable"
    start = SessionStore().load().issued_at
    gate_on.now = start + 5 * DAY
    core.reset_runtime_state()
    assert core.require_account().stale is True  # expired token, inside the grace
    gate_on.now = start + 60  # rolled back to when the token looked fresh
    core.reset_runtime_state()
    assert core.require_account().stale is True  # still treated as expired: the high-water mark


def test_editing_the_file_cannot_extend_the_period(gate_on):
    sign_in(gate_on)
    gate_on.mode = "unreachable"
    path = SessionStore().path
    data = json.loads(path.read_text())
    data["issued_at"] = gate_on.now + 100 * DAY  # a forged "last verified" date
    data["high_water"] = 0
    path.write_text(json.dumps(data))
    gate_on.now += 40 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()  # the signed token's own iat decides, not the file


def test_a_token_from_another_project_in_the_file_is_refused(gate_on):
    sign_in(gate_on)
    path = SessionStore().path
    data = json.loads(path.read_text())
    data["access_token"] = KeyPair().token()
    path.write_text(json.dumps(data))
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as err:
        core.require_account()
    assert err.value.reason == "invalid"


def test_corrupt_session_file_is_a_clean_refusal(gate_on):
    SessionStore().path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("", "{", "[]", '{"access_token": 5}', "\x00\x01"):
        SessionStore().path.write_text(junk)
        core.reset_runtime_state()
        with pytest.raises(AccountRequired):
            core.require_account()


def test_app_sessions_hold_no_refresh_token_and_are_never_refreshed_by_python(gate_on):
    sign_in(gate_on, via="app")
    data = json.loads(SessionStore().path.read_text())
    assert data["source"] == "app" and data["refresh_token"] is None
    gate_on.now += 2 * 3600  # expired; the app is not running to renew it
    core.reset_runtime_state()
    gate_on.calls.clear()
    account = core.require_account()  # inside the offline grace: accepted, flagged stale
    assert account.stale is True
    assert not any(
        "grant_type" in path for _, path in gate_on.calls
    )  # no rotation of the app's chain
    gate_on.now += 31 * DAY
    core.reset_runtime_state()
    with pytest.raises(AccountRequired):
        core.require_account()


def test_a_newer_app_token_replaces_the_file_and_an_older_one_does_not(gate_on):
    newer = gate_on.keys.token(iat=gate_on.now)
    older = gate_on.keys.token(iat=gate_on.now - 1800)
    core.verify_presented_token(newer, persist=True)
    core.verify_presented_token(older, persist=True)
    assert SessionStore().load().access_token == newer
    later = gate_on.keys.token(iat=gate_on.now + 100)
    gate_on.now += 100
    core.verify_presented_token(later, persist=True)
    assert SessionStore().load().access_token == later


def test_web_mode_never_persists(gate_on):
    core.verify_presented_token(gate_on.keys.token(), persist=False)
    assert not SessionStore().path.exists()


def test_logout_removes_the_file_and_ends_the_server_session(gate_on):
    sign_in(gate_on)
    assert core.logout() is True
    assert not SessionStore().path.exists()
    assert any(path.endswith("/auth/v1/logout?scope=local") for _, path in gate_on.calls)
    assert core.logout() is False
    with pytest.raises(AccountRequired):
        core.require_account()


def test_logout_works_offline(gate_on):
    sign_in(gate_on)
    gate_on.mode = "unreachable"
    assert core.logout() is True
    assert not SessionStore().path.exists()


# -- the file store -----------------------------------------------------------------------------


def test_concurrent_updates_do_not_lose_writes(gate_on):
    sign_in(gate_on)
    errors: list[BaseException] = []

    def bump():
        try:
            store = SessionStore()  # every thread has its own handle, like separate programs
            for _ in range(10):

                def change(cur: StoredSession | None) -> StoredSession | None:
                    assert cur is not None
                    cur.high_water += 1
                    return cur

                store.update(change)
        except BaseException as exc:
            errors.append(exc)

    base = SessionStore().load().high_water
    threads = [threading.Thread(target=bump) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert SessionStore().load().high_water == base + 80
    assert json.loads(SessionStore().path.read_text())["user_id"]  # never torn


def test_concurrent_processes_do_not_corrupt_the_file(gate_on, tmp_path):
    """Two real processes writing the same file at once (an app engine and a command line)."""
    import subprocess

    sign_in(gate_on)
    code = (
        "import sys\n"
        "from engine.account.store import SessionStore\n"
        "s = SessionStore()\n"
        "for _ in range(25):\n"
        "    def ch(cur):\n"
        "        cur.high_water += 1\n"
        "        return cur\n"
        "    s.update(ch)\n"
    )
    env = {**os.environ, "RADARX_CONFIG_DIR": os.environ["RADARX_CONFIG_DIR"]}
    base = SessionStore().load().high_water
    cwd = os.path.dirname(os.path.dirname(__file__))
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, cwd=cwd) for _ in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    assert SessionStore().load().high_water == base + 100


def test_no_temporary_files_are_left_behind(gate_on):
    sign_in(gate_on)
    for _ in range(5):
        SessionStore().update(lambda cur: cur)
    leftovers = [p.name for p in SessionStore().directory.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_config_dir_follows_the_platform(monkeypatch):
    from pathlib import Path

    from engine.account import store

    monkeypatch.delenv("RADARX_CONFIG_DIR", raising=False)
    monkeypatch.setattr(store.sys, "platform", "darwin")
    assert str(store.config_dir()).endswith("Library/Application Support/radarx-desktop")
    monkeypatch.setattr(store.sys, "platform", "linux")
    monkeypatch.setattr(store.os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert store.config_dir() == Path("/xdg/radarx-desktop")
    # (the Windows branch, %APPDATA%, cannot be built on POSIX: pathlib refuses WindowsPath)


def test_errors_never_print_secrets(gate_on):
    token = gate_on.keys.token()
    with pytest.raises(tokens.TokenInvalid) as err:
        tokens.verify_access_token(token[:-4] + "AAAA", now=gate_on.now)
    assert token not in str(err.value) and token not in repr(err.value)
