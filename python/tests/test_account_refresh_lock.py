"""The renewal lock in detail (store._RefreshLock) and what renewal does on errors.
The cross-process behaviour is in test_account_refresh_stress.py."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from engine.account import core
from engine.account.errors import AccountRequired
from engine.account.store import SessionStore, StoredSession, _RefreshLock


def lock_file(directory):
    return directory / "session.refresh.lock"


def write_lock(directory, **info):
    lock_file(directory).write_text(json.dumps(info))


def test_the_lock_is_exclusive_and_cleans_up(tmp_path):
    with _RefreshLock(tmp_path, timeout=2):
        info = json.loads(lock_file(tmp_path).read_text())
        assert info["pid"] == os.getpid() and info["host"] == socket.gethostname()
        assert info["token"] and abs(info["time"] - time.time()) < 5
        assert oct(lock_file(tmp_path).stat().st_mode & 0o777) == "0o600"
        started = time.monotonic()
        with pytest.raises(TimeoutError), _RefreshLock(tmp_path, timeout=0.3):
            pytest.fail("a second holder got the lock")
        assert 0.25 < time.monotonic() - started < 2
        assert lock_file(tmp_path).exists()  # the failed waiter did not remove the holder's file
    assert not lock_file(tmp_path).exists()
    with _RefreshLock(tmp_path, timeout=1):  # and it can be taken again
        pass


def test_threads_exclude_each_other_even_when_flock_does_nothing(tmp_path, monkeypatch):
    from engine.account import store

    monkeypatch.setattr(store._FileLock, "_try_lock", lambda self: None)
    inside = {"now": 0, "max": 0, "entered": 0}
    guard = threading.Lock()

    def work():
        with _RefreshLock(tmp_path, timeout=20):
            with guard:
                inside["now"] += 1
                inside["entered"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(0.01)
            with guard:
                inside["now"] -= 1

    threads = [threading.Thread(target=work) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inside["entered"] == 32 and inside["max"] == 1


def test_a_lock_left_by_a_dead_process_on_this_host_is_taken_over_at_once(tmp_path):
    import subprocess
    import sys

    dead = subprocess.Popen([sys.executable, "-c", "pass"], close_fds=False)
    dead.wait()
    write_lock(tmp_path, host=socket.gethostname(), pid=dead.pid, time=time.time(), token="old")
    started = time.monotonic()
    with _RefreshLock(tmp_path, timeout=5):
        assert json.loads(lock_file(tmp_path).read_text())["token"] != "old"
    assert time.monotonic() - started < 2


def test_a_live_holder_on_this_host_is_not_robbed_while_fresh(tmp_path):
    write_lock(tmp_path, host=socket.gethostname(), pid=os.getpid(), time=time.time(), token="me")
    with pytest.raises(TimeoutError), _RefreshLock(tmp_path, timeout=0.4):
        pass
    assert json.loads(lock_file(tmp_path).read_text())["token"] == "me"


def test_a_fresh_lock_from_another_computer_is_respected(tmp_path):
    write_lock(tmp_path, host="node-17", pid=4242, time=time.time(), token="theirs")
    with pytest.raises(TimeoutError), _RefreshLock(tmp_path, timeout=0.4):
        pass
    assert json.loads(lock_file(tmp_path).read_text())["token"] == "theirs"


def test_an_old_lock_from_another_computer_is_taken_over_after_the_stale_age(tmp_path):
    write_lock(tmp_path, host="node-17", pid=4242, time=time.time() - 500, token="theirs")
    old = time.time() - 500
    os.utime(lock_file(tmp_path), (old, old))
    with _RefreshLock(tmp_path, timeout=5):
        assert json.loads(lock_file(tmp_path).read_text())["token"] != "theirs"


def test_a_lock_file_being_written_right_now_is_not_stale(tmp_path):
    lock_file(tmp_path).write_text("")  # created, content not written yet
    with pytest.raises(TimeoutError), _RefreshLock(tmp_path, timeout=0.3):
        pass


def test_release_only_removes_the_lock_if_it_is_still_ours(tmp_path):
    lock = _RefreshLock(tmp_path, timeout=2)
    with lock:
        write_lock(tmp_path, host="x", pid=1, time=time.time(), token="taken-over")
    assert json.loads(lock_file(tmp_path).read_text())["token"] == "taken-over"


def test_breaking_a_stale_lock_never_destroys_a_fresh_one(tmp_path):
    """Two waiters both saw lock T1 as stale; the first replaced it with a fresh T2; the second
    must not delete T2."""
    waiter = _RefreshLock(tmp_path, timeout=2)
    write_lock(tmp_path, host="a", pid=1, time=time.time(), token="T2-fresh")
    waiter._break_stale({"token": "T1-stale"})  # what the second waiter saw earlier
    assert json.loads(lock_file(tmp_path).read_text())["token"] == "T2-fresh"
    assert not list(tmp_path.glob("session.refresh.lock.stale.*"))


def test_the_lock_directory_is_created_private(tmp_path):
    target = tmp_path / "new" / "cfg"
    with _RefreshLock(target, timeout=2):
        assert target.is_dir()


# -- renewal behaviour ---------------------------------------------------------------------------


def expired_cli_session(gate_on, refresh="refresh-0", *, hours=2):
    store = SessionStore()
    token = gate_on.keys.token(iat=gate_on.now - hours * 3600)
    store.save(
        StoredSession(
            access_token=token,
            refresh_token=refresh,
            user_id="11111111-2222-3333-4444-555555555555",
            email="ada@example.test",
            source="cli",
            issued_at=gate_on.now - hours * 3600,
            high_water=gate_on.now,
        )
    )
    return store


def test_a_timeout_on_the_lock_keeps_the_session_and_calls_nobody(gate_on):
    store = expired_cli_session(gate_on)
    gate_on.calls.clear()
    holder_ready, release = threading.Event(), threading.Event()

    def hold():
        with _RefreshLock(store.directory, timeout=5):
            holder_ready.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    holder_ready.wait(5)
    real = _RefreshLock.__init__

    def short(self, directory, timeout=None, stale_after=None, skip=None):
        real(self, directory, 0.3, stale_after, skip)

    _RefreshLock.__init__ = short  # type: ignore[method-assign]
    try:
        account = core.check_stored_session(store)  # waits 0.3 s, then falls back to what is stored
    finally:
        _RefreshLock.__init__ = real  # type: ignore[method-assign]
        release.set()
        t.join()
    assert account.stale and store.load() is not None
    assert not any("grant_type=refresh_token" in c[1] for c in gate_on.calls)


def test_a_transient_server_error_never_deletes_the_session(gate_on):
    from engine.account.errors import AccountServerError

    store = expired_cli_session(gate_on)
    original = store.load()

    def broken(method, url, **kw):
        if "grant_type=refresh_token" in url:
            raise AccountServerError(503, "", "Service Unavailable")
        return gate_on.request(method, url, **kw)

    from engine.account import http

    http.request_json = broken  # restored by the fixture's monkeypatch teardown order below
    try:
        account = core.check_stored_session(store)
    finally:
        http.request_json = gate_on.request
    assert account.stale
    kept = store.load()
    assert kept is not None and kept.refresh_token == original.refresh_token
    assert store.network_down_recently(core.now(), 15)


def test_a_refusal_deletes_the_session_only_if_the_file_still_holds_the_refused_token(gate_on):
    # (a) the file holds the refused token -> the session is gone and the message says so
    store = expired_cli_session(gate_on, refresh="not-a-token")
    with pytest.raises(AccountRequired) as caught:
        core.check_stored_session(store)
    assert caught.value.reason == "revoked" and store.load() is None

    # (b) somebody saved a different refresh token while we were asking: it must survive
    store = expired_cli_session(gate_on, refresh="not-a-token")
    real = core.gotrue.refresh

    def refuse_and_meanwhile_replace(token, timeout=0, late=None):
        newer = store.load()
        newer.refresh_token = "refresh-7"
        store._write(newer)  # (we already hold the lock here, standing in for another computer)
        return real(token, timeout=timeout, late=late)

    core.gotrue.refresh = refuse_and_meanwhile_replace
    try:
        core.check_stored_session(store)
    finally:
        core.gotrue.refresh = real
    kept = store.load()
    assert kept is not None and kept.refresh_token == "refresh-7"


def test_the_new_tokens_are_saved_before_they_are_checked(gate_on, monkeypatch):
    """A verification problem after the server rotated the token must not lose the new refresh
    token (the old one is spent)."""
    store = expired_cli_session(gate_on)

    seen = {}

    real_verify = core._verify

    def verify(token, at, source="cli"):
        seen["on_disk"] = store.load().refresh_token
        if seen.get("count", 0) >= 1:  # the check of the NEW token fails
            raise AccountRequired("invalid")
        seen["count"] = seen.get("count", 0) + 1
        return real_verify(token, at, source)

    monkeypatch.setattr(core, "_verify", verify)
    with pytest.raises(AccountRequired):
        core.check_stored_session(store)
    assert store.load().refresh_token == "refresh-1"  # the rotated token is on disk
    assert seen["on_disk"] == "refresh-1"
