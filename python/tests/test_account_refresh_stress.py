"""Renewing the session under parallelism (docs/ACCOUNTS.md "Renewal under parallelism").

Supabase rotates refresh tokens: each may be used ONCE; a second use looks like theft and ends the
whole session. Many jobs starting together on a shared home folder must therefore renew the session
exactly once. These tests run real child processes (and threads) against ``RotatingGoTrue``, a mock
of the account server that implements rotation with reuse detection, and count what the server saw.
The lock under test must work even where ``flock`` does not (``--broken-flock``: a network file
system without a lock daemon). Nothing touches a real network or a real account.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from engine.account.store import SessionStore, StoredSession
from tests.account_helpers import spawn_python
from tests.gotrue_http import USER_ID, RotatingGoTrue

PYTHON_DIR = Path(__file__).resolve().parents[1]
#: Where the child processes import ``engine`` from: normally this tree. Pointing it at an older
#: copy of the code (RADARX_TEST_ENGINE_TREE=/path/to/python) shows how that code behaves.
ENGINE_TREE = Path(os.environ.get("RADARX_TEST_ENGINE_TREE") or PYTHON_DIR)
WORKER = str(PYTHON_DIR / "tests" / "refresh_worker.py")


def child_env(server: RotatingGoTrue, cfg: Path) -> dict[str, str]:
    return {
        "PYTHONPATH": str(ENGINE_TREE),
        "PATH": "/usr/bin:/bin",
        "HOME": str(cfg.parent),
        "RADARX_CONFIG_DIR": str(cfg),
        "RADARX_SUPABASE_URL": server.url,
        "RADARX_SUPABASE_KEY": "sb_publishable_test_only",
        "RADARX_ENTITLEMENT_JWKS": json.dumps(server.ent.jwks),
    }


def expire_session(server: RotatingGoTrue, cfg: Path, *, first: bool = False) -> StoredSession:
    """Make the stored access token expired, keeping the refresh token that is current."""
    store = SessionStore(cfg)
    if first:
        session = server.new_session(iat=time.time() - 7200)
        refresh = session["refresh_token"]
    else:
        current = store.load()
        assert current is not None, "the session file is gone"
        refresh = current.refresh_token
    stored = StoredSession(
        access_token=server.access_token("ada@example.test", iat=time.time() - 7200),
        refresh_token=refresh,
        user_id=USER_ID,
        email="ada@example.test",
        source="cli",
        issued_at=time.time() - 7200,
        high_water=time.time(),
    )
    store.save(stored)
    return stored


def run_round(
    server,
    cfg,
    *,
    processes: int,
    threads: int,
    broken_flock: bool,
    extra=(),
    checks: int = 1,
    keychain=None,
) -> list[str]:
    go_at = time.time() + 2.5
    procs = []
    for _ in range(processes):
        cmd = [WORKER, "--threads", str(threads), "--go-at", str(go_at), "--checks", str(checks)]
        if broken_flock:
            cmd.append("--broken-flock")
        env = child_env(server, cfg)
        if keychain is not None:
            cmd += ["--file-keychain", str(keychain)]
            env["RADARX_SECRET_STORE"] = "auto"
        procs.append(
            spawn_python(
                [*cmd, *extra],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    outcomes: list[str] = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        outcomes += json.loads(out.strip().splitlines()[-1])["results"]
    return outcomes


@pytest.fixture()
def server():
    with RotatingGoTrue(latency=0.15) as srv:
        yield srv


@pytest.mark.parametrize("broken_flock", [False, True], ids=["flock", "flock-does-nothing"])
def test_eight_processes_renew_exactly_once_per_expiry(server, tmp_path, broken_flock):
    cfg = tmp_path / "cfg"
    rounds = 3
    for n in range(rounds):
        expire_session(server, cfg, first=n == 0)
        outcomes = run_round(server, cfg, processes=8, threads=1, broken_flock=broken_flock)
        assert outcomes == ["ok"] * 8, outcomes
        assert server.refresh_calls == n + 1, (n, server.refresh_calls)
    assert server.reuse_events == [] and server.revoked == set()
    assert server.refused == []
    final = SessionStore(cfg).load()
    assert (
        final is not None
        and final.refresh_token == f"rt-{next(iter(_families(server)))}-{rounds + 1}"
    )


def _families(server):
    return {rec["family"] for rec in server._tokens.values()}


@pytest.mark.parametrize("broken_flock", [False, True], ids=["flock", "flock-does-nothing"])
def test_thirty_two_threads_in_one_process_renew_once(server, tmp_path, broken_flock):
    cfg = tmp_path / "cfg"
    expire_session(server, cfg, first=True)
    outcomes = run_round(server, cfg, processes=1, threads=32, broken_flock=broken_flock)
    assert outcomes == ["ok"] * 32
    assert server.refresh_calls == 1
    assert server.reuse_events == [] and server.revoked == set()


def test_eight_processes_with_four_threads_each_and_a_broken_flock(server, tmp_path):
    """8 processes x 4 threads = 32 workers, flock useless: still one renewal per expiry."""
    cfg = tmp_path / "cfg"
    for n in range(2):
        expire_session(server, cfg, first=n == 0)
        outcomes = run_round(server, cfg, processes=8, threads=4, broken_flock=True, checks=3)
        assert outcomes == ["ok"] * 96
        assert server.refresh_calls == n + 1
    assert server.reuse_events == [] and server.revoked == set()
    assert SessionStore(cfg).load() is not None


def test_without_the_renewal_lock_the_mock_server_does_catch_a_reuse(server, tmp_path):
    """The control: with the renewal lock removed (and flock useless) the same herd DOES trigger
    reuse detection and the server ends the session. This proves the tests above can fail."""
    cfg = tmp_path / "cfg"
    expire_session(server, cfg, first=True)
    run_round(server, cfg, processes=8, threads=2, broken_flock=True, extra=["--no-refresh-lock"])
    assert server.reuse_events, "the herd should have used a refresh token twice"
    assert server.revoked, "the server should have revoked the session family"
    assert server.refresh_calls > 1


def test_a_transient_outage_does_not_delete_the_session_or_make_everyone_wait(server, tmp_path):
    cfg = tmp_path / "cfg"
    first = expire_session(server, cfg, first=True)
    server.offline = True
    started = time.monotonic()
    outcomes = run_round(server, cfg, processes=8, threads=1, broken_flock=False)
    took = time.monotonic() - started
    # the expired token is still inside the 30-day offline allowance: every check passes
    assert outcomes == ["ok"] * 8
    kept = SessionStore(cfg).load()
    assert kept is not None and kept.refresh_token == first.refresh_token
    # only the first process hit the server: the others saw the marker and did not try again
    assert server.refresh_calls <= 2, server.refresh_calls
    assert took < 30
    server.offline = False
    server.refresh_calls = 0
    # once the server is back (and the marker has aged out) the same session renews fine
    for marker in cfg.glob("session.netdown"):
        marker.write_text(repr(time.time() - 3600))
    assert run_round(server, cfg, processes=2, threads=1, broken_flock=False) == ["ok"] * 2
    assert server.refresh_calls == 1 and server.revoked == set()


def expire_token_session(server, cfg, pat_token):
    """A stored access-token session whose one-hour entitlement token has run out."""
    old = server.ent.token(
        sub=USER_ID, iat=time.time() - 7200, pat_expires=time.time() + 30 * 86400, name="stress"
    )
    SessionStore(cfg).save(
        StoredSession(
            access_token=old,
            pat_token=pat_token,
            user_id=USER_ID,
            email="ada@example.test",
            source="pat",
            issued_at=time.time() - 7200,
            high_water=time.time(),
        )
    )


@pytest.mark.parametrize("broken_flock", [False, True], ids=["flock", "flock-does-nothing"])
def test_a_herd_of_jobs_with_one_access_token_exchanges_it_once(server, tmp_path, broken_flock):
    """Access tokens do not rotate, but the exchange is rate limited on the server: a cluster of
    jobs sharing one session file must not each call it."""
    cfg = tmp_path / "cfg"
    pat_token = server.create_pat(name="stress", days=30)
    for n in range(2):
        expire_token_session(server, cfg, pat_token)
        outcomes = run_round(
            server, cfg, processes=8, threads=4, broken_flock=broken_flock, checks=2
        )
        assert outcomes == ["ok"] * 64, outcomes
        assert server.exchange_calls == n + 1, (n, server.exchange_calls)
    kept = SessionStore(cfg).load()
    assert kept is not None and kept.pat_token == pat_token and kept.source == "pat"


def test_the_renewal_lock_still_serialises_when_the_session_is_in_the_keychain(
    server, tmp_path, monkeypatch
):
    """The tokens live in a (fake, shared) keychain; the lock stays a file, so 8 processes x 4 threads
    still renew exactly once per expiry, and no token ever lands in the session file."""
    from engine.account import vault
    from tests.keychain_helpers import FileKeychain, install

    cfg = tmp_path / "cfg"
    shared = tmp_path / "keychain.json"
    install(monkeypatch, FileKeychain(shared))  # the parent seeds it the same way
    for n in range(2):
        expire_session(server, cfg, first=n == 0)
        assert SessionStore(cfg).load().storage == "keychain"
        outcomes = run_round(
            server, cfg, processes=8, threads=4, broken_flock=True, checks=2, keychain=shared
        )
        assert outcomes == ["ok"] * 64, outcomes
        assert server.refresh_calls == n + 1
    assert server.reuse_events == [] and server.revoked == set()
    final = SessionStore(cfg).load()
    assert (
        final.storage == "keychain"
        and final.refresh_token == "rt-" + next(iter(_families(server))) + f"-{3}"
    )
    text = (cfg / "session.json").read_text()
    assert final.refresh_token not in text and final.access_token not in text
    vault.reset()
