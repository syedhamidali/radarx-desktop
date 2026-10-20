"""Regression tests for the second independent review of the login / token / keychain work
(each one failed before its fix; see docs/ACCOUNTS.md for the behaviour they pin down)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from engine.account import core, pat, vault
from engine.account.errors import AccountRequired, AccountServerError
from engine.account.store import SessionStore, StoredSession
from tests.account_helpers import new_pat, run_python, spawn_python
from tests.gotrue_http import USER_ID, RotatingGoTrue
from tests.keychain_helpers import FakeKeychain, install

DAY = 86400
PYTHON_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def keychain(monkeypatch):
    fake = FakeKeychain()
    install(monkeypatch, fake)
    yield fake
    vault.reset()


def lock_keychain(fake: FakeKeychain) -> None:
    """Reads and writes start failing, as for a locked collection or an unanswered prompt."""
    fake.fail = "locked"
    vault.reset()


def unlock_keychain(fake: FakeKeychain) -> None:
    fake.fail = None
    vault.reset()


def token_session(gate_on, **extra) -> StoredSession:
    return StoredSession(
        access_token=gate_on.ent.token(iat=gate_on.now, pat_expires=gate_on.now + 30 * DAY),
        pat_token=new_pat(),
        user_id="11111111-2222-3333-4444-555555555555",
        email="ada@example.test",
        source="pat",
        issued_at=gate_on.now,
        high_water=gate_on.now,
        **extra,
    )


# -- 1. a keychain that cannot be READ must never cost an access-token session -------------------


def test_the_app_handing_over_a_token_cannot_replace_a_token_session_that_cannot_be_read(
    gate_on, keychain
):
    store = SessionStore()
    session = token_session(gate_on)
    store.save(session)
    assert store.load().storage == "keychain"
    lock_keychain(keychain)
    core.verify_presented_token(gate_on.keys.token(), persist=True)  # the connection is accepted
    marker = json.loads(store.path.read_text())
    assert marker["source"] == "pat" and marker["storage"] == "keychain", "the file was replaced"
    unlock_keychain(keychain)
    kept = store.load()
    assert kept.source == "pat" and kept.pat_token == session.pat_token


def test_the_app_signing_out_cannot_delete_a_token_session_that_cannot_be_read(gate_on, keychain):
    store = SessionStore()
    session = token_session(gate_on)
    store.save(session)
    lock_keychain(keychain)
    core.end_presented_session()
    assert store.path.exists(), "the sign-out deleted the session file"
    unlock_keychain(keychain)
    assert store.load().pat_token == session.pat_token
    assert keychain.items, "the keychain entry was removed"


def test_signing_out_in_the_app_still_removes_an_ordinary_session_whose_keychain_is_locked(
    gate_on, keychain
):
    """The guard is about access-token sessions only: the app's own sign-in goes as before."""
    core.verify_presented_token(gate_on.keys.token(), persist=True)
    lock_keychain(keychain)
    core.end_presented_session()
    assert not SessionStore().path.exists()


def test_a_session_that_cannot_be_read_does_not_switch_off_the_clock_rule(gate_on, keychain):
    """The latest clock reading is in the file, not in the keychain: a failed read must not make
    the rule vanish (an expired token inside the allowance only because the read failed)."""
    store = SessionStore()
    old = gate_on.keys.token(iat=gate_on.now - 29 * DAY)
    store.save(
        StoredSession(
            access_token=old,
            user_id="11111111-2222-3333-4444-555555555555",
            email="ada@example.test",
            source="app",
            issued_at=gate_on.now - 29 * DAY,
            high_water=gate_on.now + 5 * DAY,  # the clock has been seen 5 days ahead
        )
    )
    lock_keychain(keychain)
    with pytest.raises(AccountRequired) as caught:
        core.verify_presented_token(old, persist=True)
    assert caught.value.reason == "offline_expired"


def test_updating_a_session_that_cannot_be_read_raises_instead_of_overwriting(gate_on, keychain):
    store = SessionStore()
    store.save(token_session(gate_on))
    lock_keychain(keychain)
    seen = []
    with pytest.raises(OSError):
        store.update(lambda cur: seen.append(cur) or token_session(gate_on))
    assert seen == [], "the change was applied to 'nothing' because the read failed"


def test_a_refusal_from_the_server_does_not_delete_a_session_that_cannot_be_read(gate_on, keychain):
    store = SessionStore()
    token = gate_on.keys.token(iat=gate_on.now - 7200)
    store.save(
        StoredSession(
            access_token=token,
            refresh_token="not-a-token",
            user_id="11111111-2222-3333-4444-555555555555",
            email="ada@example.test",
            source="cli",
            issued_at=gate_on.now - 7200,
            high_water=gate_on.now,
        )
    )
    real = SessionStore.load_strict
    state = {"n": 0}

    def flaky(self):
        state["n"] += 1
        if state["n"] >= 3:  # the first reads work; the one after the refusal fails
            raise vault.VaultError("locked")
        return real(self)

    SessionStore.load_strict = flaky  # type: ignore[method-assign]
    try:
        core.check_stored_session(store)
    except AccountRequired:
        pass
    finally:
        SessionStore.load_strict = real  # type: ignore[method-assign]
    assert store.path.exists() and keychain.items


# -- 6. a failed migration puts the file back exactly --------------------------------------------


class ReadRefusing(FakeKeychain):
    """Accepts writes, then refuses to read the entry back (a denied macOS prompt)."""

    armed = False

    def get_password(self, service, username):
        if self.armed and not username.startswith("probe-"):
            raise RuntimeError("denied")
        return super().get_password(service, username)


def test_a_migration_whose_read_back_is_refused_leaves_the_file_byte_for_byte(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    store = SessionStore()
    store.save(
        StoredSession(
            access_token="A" * 900,
            refresh_token="R" * 40,
            user_id="u",
            email="ada@example.test",
            source="cli",
        )
    )
    original = store.path.read_bytes()
    fake = ReadRefusing()
    install(monkeypatch, fake)
    real_write = vault.write

    def write_then_refuse_reads(*a, **k):
        out = real_write(*a, **k)
        fake.armed = True
        return out

    monkeypatch.setattr(vault, "write", write_then_refuse_reads)
    assert store.migrate_to_keychain() is False
    assert store.path.read_bytes() == original, "the secrets were stripped from the file"
    assert not fake.items, "an unverified copy was left in the keychain"
    fake.armed = False
    assert SessionStore().load().refresh_token == "R" * 40


def test_a_migration_that_blows_up_half_way_leaves_the_file_byte_for_byte(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    store = SessionStore()
    store.save(
        StoredSession(
            access_token="A" * 900, refresh_token="R" * 40, user_id="u", email="e@x", source="cli"
        )
    )
    original = store.path.read_bytes()
    fake = FakeKeychain()
    install(monkeypatch, fake)
    monkeypatch.setattr(
        SessionStore, "load_strict", lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):  # not an OSError: it is not swallowed either
        store.migrate_to_keychain()
    monkeypatch.undo()
    assert store.path.read_bytes() == original


def test_a_session_already_in_the_keychain_is_recognised_without_taking_the_lock(
    gate_on, keychain, monkeypatch
):
    store = SessionStore()
    store.save(token_session(gate_on))
    monkeypatch.setattr(
        SessionStore, "locked", lambda self: (_ for _ in ()).throw(AssertionError("took the lock"))
    )
    assert store.migrate_to_keychain() is False


# -- 7. RADARX_SECRET_STORE=file after a keychain sign-in ------------------------------------------


def test_file_mode_after_a_keychain_sign_in_says_so_and_logout_still_works(
    gate_on, keychain, monkeypatch
):
    store = SessionStore()
    store.save(token_session(gate_on))
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    with pytest.raises(OSError, match="RADARX_SECRET_STORE=file"):
        store.load_strict()
    with pytest.raises(AccountRequired) as caught:
        core.check_stored_session(store)
    assert "RADARX_SECRET_STORE=file" in str(caught.value) and "unset it" in str(caught.value)
    assert core.logout(store) is True  # signing out always works
    assert not store.path.exists() and not keychain.items


# -- 5. a refresh token is spent by use: never start one with a short budget ---------------------


def expired_cli_session(gate_on) -> SessionStore:
    store = SessionStore()
    store.save(
        StoredSession(
            access_token=gate_on.keys.token(iat=gate_on.now - 7200),
            refresh_token="refresh-0",
            user_id="11111111-2222-3333-4444-555555555555",
            email="ada@example.test",
            source="cli",
            issued_at=gate_on.now - 7200,
            high_water=gate_on.now,
        )
    )
    return store


@pytest.mark.parametrize("budget", [0.2, 1.0, 2.5, 9.0])
def test_a_refresh_is_not_started_with_a_short_budget(gate_on, budget):
    store = expired_cli_session(gate_on)
    gate_on.calls.clear()
    account = core.check_stored_session(store, net_timeout=budget)
    assert account.stale  # the stored session is used as it is
    assert not any("grant_type=refresh_token" in c[1] for c in gate_on.calls)
    assert store.load().refresh_token == "refresh-0"


def test_with_enough_budget_the_refresh_happens(gate_on):
    store = expired_cli_session(gate_on)
    account = core.check_stored_session(store, net_timeout=12.0)
    assert not account.stale and store.load().refresh_token == "refresh-1"


def test_an_answer_that_arrives_after_the_client_gave_up_is_kept(tmp_path, monkeypatch):
    """The server rotates the token and the reply is slower than the client's deadline: the old
    refresh token is spent, so the late reply holds the only valid one and must be saved."""
    with RotatingGoTrue(latency=1.0) as server:
        monkeypatch.setenv("RADARX_SUPABASE_URL", server.url)
        monkeypatch.setenv("RADARX_SUPABASE_KEY", "sb_publishable_test_only")
        monkeypatch.setenv("RADARX_CONFIG_DIR", str(tmp_path / "cfg"))
        from engine.account import tokens

        tokens.reset_cache()
        first = server.new_session(iat=time.time() - 7200)
        store = SessionStore()
        store.save(
            StoredSession(
                access_token=first["access_token"],
                refresh_token=first["refresh_token"],
                user_id=USER_ID,
                email="ada@example.test",
                source="cli",
                issued_at=time.time() - 7200,
                high_water=time.time(),
            )
        )
        seen = store.load()
        assert core._refresh_stored(store, seen, time.time(), time.time(), timeout=0.3) is None
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and store.load().refresh_token == first["refresh_token"]:
            time.sleep(0.1)
        kept = store.load().refresh_token
        assert kept != first["refresh_token"], "the late reply (the only valid token) was lost"
        assert server.refresh_calls == 1 and server.reuse_events == []
        # and the next renewal works: it uses the new token, no reuse, no revocation
        server.latency = 0.0
        stale = StoredSession(**{**store.load().__dict__})
        stale.access_token = server.access_token("ada@example.test", iat=time.time() - 7200)
        store.save(stale)
        core.reset_runtime_state()  # (the failed attempt noted a short back-off)
        account = core.check_stored_session(store)
        assert not account.stale and server.reuse_events == [] and server.revoked == set()
        tokens.reset_cache()


# -- 3. RADARX_TOKEN: rate limits, back-off and a cache for loops of commands ---------------------


def test_a_rate_limited_exchange_is_retried_with_the_servers_wait(monkeypatch):
    answers = [
        AccountServerError(429, "rate_limited", "", retry_after=3.0),
        AccountServerError(429, "rate_limited", "", retry_after=3.0),
        {"entitlement_token": "x.y.z"},
    ]
    waits = []

    def fake_exchange(token, timeout=0):
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(pat, "exchange", fake_exchange)
    out = pat.exchange_with_retry(new_pat(), sleep=waits.append)
    assert out["entitlement_token"] == "x.y.z"
    assert len(waits) == 2 and all(3.0 <= w <= 3.0 * 1.4 + 0.01 for w in waits)


def test_retrying_is_bounded_and_the_last_429_is_raised(monkeypatch):
    calls = []

    def always_limited(token, timeout=0):
        calls.append(1)
        raise AccountServerError(429, "rate_limited", "", retry_after=10.0)

    monkeypatch.setattr(pat, "exchange", always_limited)
    slept = []
    with pytest.raises(AccountServerError) as caught:
        pat.exchange_with_retry(new_pat(), budget=25.0, sleep=slept.append)
    assert caught.value.status == 429 and 2 <= len(calls) <= 3 and sum(slept) <= 25.0


def test_other_failures_are_not_retried(monkeypatch):
    calls = []

    def refused(token, timeout=0):
        calls.append(1)
        raise AccountServerError(401, "invalid_token", "")

    monkeypatch.setattr(pat, "exchange", refused)
    with pytest.raises(AccountServerError):
        pat.exchange_with_retry(new_pat(), sleep=lambda s: None)
    assert calls == [1]


def test_a_rate_limited_environment_token_says_rate_limited_not_unreachable(gate_on, monkeypatch):
    monkeypatch.setattr(pat, "RETRY_BUDGET_S", 0.0)
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    gate_on.exchange_mode = "rate_limited"
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "unavailable"
    assert "limiting access-token exchanges" in str(caught.value)
    assert "cannot be reached" not in str(caught.value)


def test_a_loop_of_processes_with_one_environment_token_exchanges_once(gate_on, monkeypatch):
    token = gate_on.create_pat()
    monkeypatch.setenv("RADARX_TOKEN", token)
    for _ in range(6):  # six separate "processes": nothing is kept in memory between them
        core.reset_runtime_state()
        assert core.require_account().token_name == "ci"
    assert gate_on.exchange_calls == 1
    cache = list(SessionStore().directory.glob("env-token-*.json"))
    assert len(cache) == 1 and oct(cache[0].stat().st_mode & 0o777) == "0o600"
    assert token not in cache[0].read_text(), "the access token itself must never be cached"
    assert token not in cache[0].name


def test_the_cache_lapses_with_the_token_and_can_be_switched_off(gate_on, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    gate_on.now += 3700
    core.reset_runtime_state()
    core.require_account()
    assert gate_on.exchange_calls == 2  # the cached token had run out
    monkeypatch.setenv("RADARX_TOKEN_CACHE", "0")
    for f in SessionStore().directory.glob("env-token-*.json"):
        f.unlink()
    for _ in range(3):
        core.reset_runtime_state()
        core.require_account()
    assert gate_on.exchange_calls == 5
    assert not list(SessionStore().directory.glob("env-token-*.json"))


def test_a_revoked_environment_token_is_not_kept_alive_by_the_cache(gate_on, monkeypatch):
    token = gate_on.create_pat()
    monkeypatch.setenv("RADARX_TOKEN", token)
    core.require_account()
    gate_on.pats[token]["revoked"] = True
    gate_on.now += 3700
    core.reset_runtime_state()
    with pytest.raises(AccountRequired) as caught:
        core.require_account()
    assert caught.value.reason == "revoked"
    assert not list(SessionStore().directory.glob("env-token-*.json"))


def test_a_forged_cache_file_is_not_trusted(gate_on, monkeypatch):
    token = gate_on.create_pat()
    monkeypatch.setenv("RADARX_TOKEN", token)
    core.require_account()
    (cache,) = SessionStore().directory.glob("env-token-*.json")
    from tests.account_helpers import EntitlementKeys

    cache.write_text(json.dumps({"entitlement_token": EntitlementKeys().token()}))  # wrong key
    core.reset_runtime_state()
    core.require_account()
    assert gate_on.exchange_calls == 2  # ignored, and replaced by a real exchange


def env_for(server: RotatingGoTrue, cfg: Path, token: str) -> dict[str, str]:
    return {
        "PYTHONPATH": str(PYTHON_DIR),
        "PATH": "/usr/bin:/bin",
        "HOME": str(cfg.parent),
        "RADARX_CONFIG_DIR": str(cfg),
        "RADARX_SUPABASE_URL": server.url,
        "RADARX_SUPABASE_KEY": "sb_publishable_test_only",
        "RADARX_ENTITLEMENT_JWKS": json.dumps(server.ent.jwks),
        "RADARX_TOKEN": token,
    }


def test_real_processes_in_a_loop_and_in_parallel_exchange_once(tmp_path):
    with RotatingGoTrue(latency=0.2) as server:
        token = server.create_pat(days=30)
        env = env_for(server, tmp_path / "cfg", token)
        base = ["-W", "ignore", "-m", "engine", "whoami", "--json"]
        for _ in range(4):  # a loop of rxd commands
            done = run_python(base, env=env)
            assert done.returncode == 0, done.stderr
        assert server.exchange_calls == 1
        # a Slurm array: many processes at once, with an empty cache
        for f in (tmp_path / "cfg").glob("env-token-*.json"):
            f.unlink()
        server.exchange_calls = 0
        procs = [
            spawn_python(base, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(8)
        ]
        for proc in procs:
            _out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
        assert server.exchange_calls == 1, server.exchange_calls
        assert sys.platform  # (documentation: the herd is serialised by the renewal lock)


# -- 4. a stale renewal lock found by many waiters must still go to exactly one -----------------


LOCK_WORKER = str(PYTHON_DIR / "tests" / "lock_worker.py")


def herd(tmp_path: Path, plant, *, processes: int = 8, rounds: int = 6) -> list[str]:
    outcomes: list[str] = []
    for n in range(rounds):
        directory = tmp_path / f"round{n}"
        directory.mkdir()
        plant(directory)
        go_at = time.time() + 2.0
        procs = [
            spawn_python(
                [LOCK_WORKER, str(directory), str(go_at), "0.15"],
                env={"PYTHONPATH": str(PYTHON_DIR), "PATH": "/usr/bin:/bin"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(processes)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            outcomes.append(out.strip())
    return outcomes


def plant_foreign_stale(directory: Path) -> None:
    import os

    lock = directory / "session.refresh.lock"
    lock.write_text(json.dumps({"host": "dead-host", "pid": 1, "time": 0, "token": "deadbeef"}))
    old = time.time() - 600
    os.utime(lock, (old, old))


def plant_dead_holder(directory: Path) -> None:
    import socket

    dead = subprocess.Popen([sys.executable, "-c", "pass"], close_fds=False)
    dead.wait()
    from engine.account import store

    (directory / "session.refresh.lock").write_text(
        json.dumps(
            {
                "host": socket.gethostname(),
                "pid": dead.pid,
                "pidns": store._pid_namespace(),
                "time": time.time(),
                "token": "dead",
            }
        )
    )


@pytest.mark.parametrize("plant", [plant_foreign_stale, plant_dead_holder], ids=["old", "dead"])
def test_a_herd_finding_a_stale_lock_never_puts_two_holders_inside(tmp_path, plant):
    outcomes = herd(tmp_path, plant)
    assert "VIOLATION" not in outcomes, outcomes.count("VIOLATION")
    assert outcomes.count("ok") == len(outcomes), set(outcomes)


def test_one_waiter_at_a_time_breaks_a_stale_lock(tmp_path):
    from engine.account.store import _RefreshLock

    plant_foreign_stale(tmp_path)
    lock = _RefreshLock(tmp_path, timeout=1)
    seen = lock._read()
    (tmp_path / "session.refresh.lock.break").write_text("")  # another waiter is breaking it
    lock._break_stale(seen)
    assert (tmp_path / "session.refresh.lock").exists(), "two waiters broke the lock at once"
    (tmp_path / "session.refresh.lock.break").unlink()
    lock._break_stale(seen)
    assert not (tmp_path / "session.refresh.lock").exists()
    assert not (tmp_path / "session.refresh.lock.break").exists()


def test_a_lock_re_created_after_it_was_seen_stale_is_not_removed(tmp_path):
    from engine.account.store import _RefreshLock

    plant_foreign_stale(tmp_path)
    lock = _RefreshLock(tmp_path, timeout=1)
    seen = lock._read()
    (tmp_path / "session.refresh.lock").write_text(
        json.dumps({"host": "h", "pid": 1, "time": time.time(), "token": "fresh"})
    )  # a faster waiter already replaced it
    lock._break_stale(seen)
    assert json.loads((tmp_path / "session.refresh.lock").read_text())["token"] == "fresh"


def test_a_wrong_clock_on_another_computer_does_not_make_a_live_lock_look_old(tmp_path):
    import os

    from engine.account.store import _RefreshLock

    lock_file = tmp_path / "session.refresh.lock"
    # the holder's clock is 10 minutes behind: what it wrote says "10 minutes ago", the file
    # server's modification time (the other clock) says "just now"
    lock_file.write_text(
        json.dumps({"host": "node-3", "pid": 77, "time": time.time() - 600, "token": "t"})
    )
    assert _RefreshLock(tmp_path)._is_stale(json.loads(lock_file.read_text())) is False
    # and the reverse: the file server's clock is behind, the holder's is right
    lock_file.write_text(
        json.dumps({"host": "node-3", "pid": 77, "time": time.time(), "token": "t"})
    )
    old = time.time() - 600
    os.utime(lock_file, (old, old))
    assert _RefreshLock(tmp_path)._is_stale(json.loads(lock_file.read_text())) is False
    # both clocks agree it is old: stale
    lock_file.write_text(json.dumps({"host": "node-3", "pid": 77, "time": old, "token": "t"}))
    os.utime(lock_file, (old, old))
    assert _RefreshLock(tmp_path)._is_stale(json.loads(lock_file.read_text())) is True


def test_a_pid_from_another_container_is_not_judged_by_this_ones_process_table(
    tmp_path, monkeypatch
):
    import socket

    from engine.account import store
    from engine.account.store import _RefreshLock

    monkeypatch.setattr(store, "_pid_namespace", lambda: "pid:[4026531836]")
    dead = subprocess.Popen([sys.executable, "-c", "pass"], close_fds=False)
    dead.wait()
    info = {
        "host": socket.gethostname(),  # containers often share a host name
        "pid": dead.pid,  # a number that means nothing in OUR pid namespace
        "pidns": "pid:[4026539999]",
        "time": time.time(),
        "token": "t",
    }
    (tmp_path / "session.refresh.lock").write_text(json.dumps(info))
    assert _RefreshLock(tmp_path)._is_stale(info) is False  # fresh, other namespace: wait for it
    info["pidns"] = "pid:[4026531836]"
    assert _RefreshLock(tmp_path)._is_stale(info) is True  # same namespace and dead: stale now


# -- 8. no crash (exit 70) when standard input is closed or ends at a prompt -----------------------


def run_cli(capsys, *argv):
    from engine import cli

    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["login", "--email", "ada@example.test", "--password-stdin"],
        ["login", "--token"],
        ["login", "--browser"],
        ["login", "--email", "ada@example.test"],
        ["login"],
    ],
    ids=["password-stdin", "token", "browser", "no-tty-email", "no-tty"],
)
def test_a_closed_standard_input_is_a_usage_error_not_a_crash(gate_on, capsys, monkeypatch, argv):
    monkeypatch.setattr("sys.stdin", None)
    code, out, err = run_cli(capsys, *argv)
    assert code == 2, (code, out, err)
    assert "Traceback" not in err and "unexpected" not in err


def test_the_end_of_input_at_a_prompt_is_a_usage_error(gate_on, capsys, monkeypatch):
    class Tty:
        def isatty(self):
            return True

        def readline(self, *a):
            return ""

    monkeypatch.setattr("sys.stdin", Tty())

    def eof(*a, **k):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    code, _o, err = run_cli(capsys, "login")  # Ctrl-D at "E-mail:"
    assert code == 2 and "end of input" in err and "unexpected" not in err
    import getpass

    monkeypatch.setattr(getpass, "getpass", eof)
    code, _o, err = run_cli(capsys, "login", "--email", "ada@example.test")  # Ctrl-D at password
    assert code == 2 and "end of input" in err
    code, _o, err = run_cli(capsys, "login", "--token")  # Ctrl-D at the token prompt
    assert code == 2 and "end of input" in err


def test_real_process_with_standard_input_closed(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(PYTHON_DIR), "HOME": str(tmp_path)}
    env["RADARX_CONFIG_DIR"] = str(tmp_path / "cfg")
    env["RADARX_SECRET_STORE"] = "file"
    for flags in ("login --email a@b.c --password-stdin", "login --token"):
        done = subprocess.run(
            ["/bin/sh", "-c", f'"{sys.executable}" -W ignore -m engine {flags} <&-'],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            close_fds=False,  # (only then can subprocess use posix_spawn: see run_python)
        )
        assert done.returncode == 2, (flags, done.returncode, done.stderr)
        assert "Traceback" not in done.stderr and "closed" in done.stderr


# -- a keychain that fails while writing is never a silent downgrade ------------------------------


def test_a_token_session_is_not_moved_into_the_file_by_a_failed_keychain_write(gate_on, keychain):
    store = SessionStore()
    session = token_session(gate_on)
    store.save(session)
    keychain.fail = "write"  # reads work, writes are refused
    vault.reset()
    vault._state.update(checked=True, usable=True, reason="", backend=keychain, name="Fake")
    renewed = token_session(gate_on)
    renewed.pat_token = session.pat_token
    with pytest.raises(OSError, match="stays where it is"):
        store.save(renewed)
    assert session.pat_token not in store.path.read_text(), "the access token went to the file"
    keychain.fail = None
    vault.reset()
    assert store.load().pat_token == session.pat_token


def test_a_rotated_refresh_token_that_falls_back_to_the_file_is_announced(
    gate_on, keychain, capsys
):
    from engine import cli
    from engine.account import store as account_store

    store = SessionStore()
    store.save(expired_cli_session(gate_on).load())
    keychain.fail = "write"
    vault.reset()
    vault._state.update(checked=True, usable=True, reason="", backend=keychain, name="Fake")
    account_store.take_storage_warnings()
    core.check_stored_session(store)  # renews: the rotated token cannot go to the keychain
    assert store.load().refresh_token == "refresh-1"
    keychain.fail = None
    code = cli.main(["whoami"])  # any command: the warning is printed after it
    err = capsys.readouterr().err
    assert code in (0, 5)
    assert "warning: the sign-in is now kept in the private file" in err
    assert account_store.take_storage_warnings() == []  # told once


# -- the loopback listener under load and with stray requests ------------------------------------


def test_sixteen_idle_connections_do_not_make_the_real_redirect_fail():
    import socket
    import threading

    from engine.account import oauth_login

    with oauth_login.LoopbackListener(wait=20) as listener:
        box = {}
        t = threading.Thread(target=lambda: box.update(r=listener.wait_for_redirect()), daemon=True)
        t.start()
        idle = [socket.create_connection(("127.0.0.1", listener.port)) for _ in range(40)]
        time.sleep(0.3)
        with socket.create_connection(("127.0.0.1", listener.port), timeout=5) as s:
            s.sendall(
                b"GET /auth/callback?code=GOOD1 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                % listener.port
            )
            reply = s.recv(100)
        assert reply.startswith(b"HTTP/1.1 200"), reply
        t.join(5)
        assert box["r"] == {"code": "GOOD1"}
        for c in idle:
            c.close()


def test_when_every_slot_is_taken_the_oldest_connection_is_dropped_not_the_new_one(monkeypatch):
    import socket
    import threading

    from engine.account import oauth_login

    monkeypatch.setattr(oauth_login, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(oauth_login, "READ_TIMEOUT_S", 30.0)  # idle ones would hold their slots
    with oauth_login.LoopbackListener(wait=20) as listener:
        box = {}
        t = threading.Thread(target=lambda: box.update(r=listener.wait_for_redirect()), daemon=True)
        t.start()
        idle = [socket.create_connection(("127.0.0.1", listener.port)) for _ in range(6)]
        time.sleep(0.4)
        with socket.create_connection(("127.0.0.1", listener.port), timeout=5) as s:
            s.sendall(
                b"GET /auth/callback?code=GOOD2 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                % listener.port
            )
            assert s.recv(100).startswith(b"HTTP/1.1 200")
        t.join(5)
        assert box["r"] == {"code": "GOOD2"}
        for c in idle:
            c.close()


def test_a_stray_redirect_that_the_server_refuses_does_not_end_the_sign_in(
    gate_on, capsys, monkeypatch
):
    """Somebody on this computer sends a code of their own first: PKCE refuses it at the exchange,
    and the sign-in goes on waiting for the real redirect."""
    import threading
    import urllib.request
    from urllib.parse import parse_qs, urlparse

    from engine.account import oauth_login
    from tests.account_helpers import fake_browser

    real = fake_browser(gate_on)
    gate_on.pkce_codes["stray"] = "someone-elses-challenge"

    def opener(url):
        redirect = parse_qs(urlparse(url).query)["redirect_to"][0]

        def late_real():
            urllib.request.urlopen(f"{redirect}?code=stray").read()  # the stray one first
            time.sleep(0.5)
            real(url)

        threading.Thread(target=late_real, daemon=True).start()
        return True

    monkeypatch.setattr(oauth_login, "open_system_browser", opener)
    monkeypatch.setattr(oauth_login, "has_display", lambda: True)
    code, out, err = run_cli(capsys, "login", "--browser", "--provider", "google")
    assert code == 0, err
    assert "Ignored a redirect" in err and "Signed in as" in out


# -- keychain leftovers hold secrets: retry, track, report ---------------------------------------


class StubbornDeletes(FakeKeychain):
    """Refuses to delete the items whose names are in ``stuck`` (until it is told otherwise)."""

    def __init__(self):
        super().__init__()
        self.stuck: set[str] = set()

    def delete_password(self, service, username):
        if any(username.endswith(f"#{n}") or n == username for n in self.stuck):
            self.calls.append(("delete", username))
            raise RuntimeError("busy")
        return super().delete_password(service, username)


def test_a_chunk_that_cannot_be_deleted_is_reported_and_the_sign_out_can_be_repeated(
    gate_on, monkeypatch
):
    fake = StubbornDeletes()
    install(monkeypatch, fake)
    store = SessionStore()
    store.save(
        token_session(
            gate_on,
        )
    )
    chunk = next(k[1] for k in fake.items if "#" in k[1])
    fake.stuck = {chunk}
    with pytest.raises(vault.VaultError, match="could not be removed"):
        store.clear()
    assert store.path.exists(), "the file (the way back to the entry) must stay"
    assert fake.items, "something is still there and the user was told"
    attempts = [c for c in fake.calls if c == ("delete", chunk)]
    assert len(attempts) >= 3, "each delete is retried"
    fake.stuck = set()
    store.clear()
    assert not store.path.exists() and not fake.items


def test_old_parts_that_could_not_be_removed_are_tracked_and_cleaned_later(gate_on, monkeypatch):
    fake = StubbornDeletes()
    install(monkeypatch, fake)
    vault.write("acct", "a" * 2000)  # 3 parts
    old_parts = [k[1] for k in fake.items if k[1].startswith("acct#")]
    fake.stuck = {old_parts[0]}
    vault.write("acct", "b" * 100)  # the old version's part 0 cannot be deleted
    assert old_parts[0] in {k[1] for k in fake.items}, "the stuck part is still there"
    header = json.loads(fake.items[("radarx-desktop", "acct")])
    assert header["stale"], "the leftover was not recorded anywhere"
    assert vault.read("acct") == "b" * 100
    fake.stuck = set()
    vault.write("acct", "c" * 100)  # the next write cleans it up
    assert old_parts[0] not in {k[1] for k in fake.items}
    vault.delete("acct")
    assert not fake.items


# -- macOS keeps the sign-in in the private file unless the keychain is asked for -----------------


def test_the_default_on_macos_is_the_file_and_the_keychain_is_opt_in(gate_on, monkeypatch):
    fake = FakeKeychain()
    monkeypatch.setattr(vault, "backend_factory", lambda: fake)
    monkeypatch.setattr("sys.platform", "darwin")
    for value in ("", "auto", "whatever"):
        monkeypatch.setenv("RADARX_SECRET_STORE", value)
        vault.reset()
        assert vault.mode() == "file", value
        usable, _name, reason = vault.status()
        assert not usable and "macOS keeps the sign-in in the private file" in reason
    store = SessionStore()
    store.save(token_session(gate_on))
    assert store.load().storage == "file" and not fake.items, "nothing may reach the keychain"
    assert "macOS keeps the sign-in" in store.load().storage_note
    store.clear()
    monkeypatch.setenv("RADARX_SECRET_STORE", "keychain")  # the opt-in
    vault.reset()
    assert vault.mode() == "auto"
    store.save(token_session(gate_on))
    assert store.load().storage == "keychain" and fake.items


def test_the_default_on_windows_and_linux_is_the_keychain(monkeypatch):
    monkeypatch.delenv("RADARX_SECRET_STORE", raising=False)
    for platform in ("win32", "linux"):
        monkeypatch.setattr("sys.platform", platform)
        assert vault.mode() == "auto", platform


def test_a_session_already_in_the_keychain_stays_usable_on_macos_without_the_opt_in(
    gate_on, monkeypatch
):
    fake = FakeKeychain()
    install(monkeypatch, fake)  # RADARX_SECRET_STORE=keychain: the session is written there
    store = SessionStore()
    session = token_session(gate_on)
    store.save(session)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.delenv("RADARX_SECRET_STORE")  # the opt-in is gone; the default is the file
    vault.reset()
    assert vault.mode() == "file"
    assert store.load().pat_token == session.pat_token, (
        "an existing keychain session must stay readable"
    )
    renewed = token_session(gate_on)
    renewed.pat_token = session.pat_token
    store.save(renewed)  # and it is written back to the keychain, not split across two places
    assert store.load().storage == "keychain"
    assert session.pat_token not in store.path.read_text()
