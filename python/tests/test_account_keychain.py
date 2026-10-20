"""The session in the operating system's keychain (docs/ACCOUNTS.md "Where the sign-in is kept"),
with a fake keychain: no test touches a real one. The 0600 file stays the fallback and must keep
working; nothing is ever downgraded without saying so."""

from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import sys
import time

import pytest

from engine import cli
from engine.account import core, store, vault
from engine.account.errors import AccountRequired
from engine.account.store import SessionStore, StoredSession
from tests.keychain_helpers import FakeKeychain, install

PASSWORD = "correct horse 1"
DAY = 86400


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def keychain(monkeypatch):
    fake = FakeKeychain()
    install(monkeypatch, fake)
    yield fake
    vault.reset()


@pytest.fixture()
def login(gate_on, capsys, monkeypatch):
    def do():
        monkeypatch.setattr(getpass, "getpass", lambda *a, **k: PASSWORD)
        code, out, err = run(capsys, "login", "--email", "ada@example.test")
        assert code == 0, err
        return out

    return do


def file_text() -> str:
    return SessionStore().path.read_text()


def tokens_of(session: StoredSession) -> list[str]:
    return [t for t in (session.access_token, session.refresh_token, session.pat_token) if t]


# -- the keychain holds the secrets, the file holds none ----------------------------------------


def test_a_login_goes_to_the_keychain_and_the_file_holds_no_secret(gate_on, keychain, login):
    out = login()
    session = SessionStore().load()
    assert session.storage == "keychain" and session.refresh_token and session.access_token
    text = file_text()
    for secret in tokens_of(session):
        assert secret not in text, "a token is still in the file"
        assert secret in keychain.everything()
    data = json.loads(text)
    assert data["storage"] == "keychain"
    assert {"access_token", "refresh_token", "pat_token"}.isdisjoint(data)
    assert data["email"] == "ada@example.test" and data["user_id"]
    assert "kept in the" in out and "the session file holds no token" in out
    assert oct(SessionStore().path.stat().st_mode & 0o777) == "0o600"  # the file is private anyway


def test_a_session_in_the_keychain_works_everywhere(gate_on, keychain, login, capsys):
    login()
    core.reset_runtime_state()
    assert core.require_account().email == "ada@example.test"
    code, out, _e = run(capsys, "modules", "--json")
    assert code == 0 and json.loads(out)
    code, out, _e = run(capsys, "whoami", "--json")
    info = json.loads(out)
    assert info["storage"] == "keychain" and info["storage_backend"]


def test_whoami_says_where_the_sign_in_is_kept(gate_on, keychain, login, capsys):
    login()
    code, out, _e = run(capsys, "whoami")
    assert code == 0 and "kept in the" in out and "keychain" in out.lower()


def test_long_values_are_split_for_keychains_with_small_items(gate_on, monkeypatch, login):
    small = FakeKeychain(max_chars=1280)  # Windows Credential Manager
    install(monkeypatch, small)
    login()
    session = SessionStore().load()
    assert session.storage == "keychain"
    assert max(len(v) for v in small.items.values()) <= 1280
    # a token longer than any item still round-trips
    long_token = gate_on.keys.token(extra={"user_metadata": {"bio": "x" * 3000}})
    vault.write("test-account", long_token)
    assert vault.read("test-account") == long_token
    assert len([k for k in small.items if k[1].startswith("test-account#")]) >= 4
    vault.delete("test-account")
    assert not [k for k in small.items if k[1].startswith("test-account")]


def test_the_account_key_differs_per_config_folder(tmp_path):
    assert SessionStore(tmp_path / "a").account != SessionStore(tmp_path / "b").account
    assert SessionStore(tmp_path / "a").account == SessionStore(tmp_path / "a").account
    assert str(tmp_path) not in SessionStore(tmp_path / "a").account  # a hash, not the path


# -- moving an existing file session into the keychain ------------------------------------------


def test_an_existing_file_session_moves_to_the_keychain_on_first_use(
    gate_on, monkeypatch, login, capsys
):
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    login()
    before = SessionStore().load()
    assert before.storage == "file" and before.refresh_token in file_text()
    fake = FakeKeychain()
    install(monkeypatch, fake)  # the keychain now works
    core.reset_runtime_state()
    code, _o, err = run(capsys, "modules", "--json")
    assert code == 0, err
    after = SessionStore().load()
    assert after.storage == "keychain"
    assert after.access_token == before.access_token and after.refresh_token == before.refresh_token
    for secret in tokens_of(before):
        assert secret not in file_text() and secret in fake.everything()


def test_nothing_moves_when_the_keychain_does_not_work_and_the_file_keeps_working(
    gate_on, monkeypatch, login, capsys
):
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    login()
    original = file_text()
    broken = FakeKeychain()
    broken.fail = "locked"
    install(monkeypatch, broken)
    core.reset_runtime_state()
    code, _o, err = run(capsys, "modules", "--json")
    assert code == 0, err
    assert file_text() == original, "the file must be untouched"
    code, out, _e = run(capsys, "whoami", "--json")
    info = json.loads(out)
    assert info["storage"] == "file" and "keychain" in info["storage_note"]
    code, out, _e = run(capsys, "whoami")
    assert "private file" in out and "keychain is not used" in out


def test_a_keychain_that_does_not_give_the_value_back_leaves_the_file_as_it_was(
    gate_on, monkeypatch, login, capsys
):
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    login()
    original = file_text()
    forgetful = FakeKeychain()
    forgetful.forget = True
    install(monkeypatch, forgetful)
    core.reset_runtime_state()
    code, _o, err = run(capsys, "modules", "--json")
    assert code == 0, err
    assert json.loads(file_text())["refresh_token"] == json.loads(original)["refresh_token"]
    assert SessionStore().load().storage == "file"
    assert not forgetful.items, "the copy that could not be verified must not stay in the keychain"


def test_no_migration_happens_when_the_keychain_is_switched_off(
    gate_on, monkeypatch, login, capsys
):
    fake = FakeKeychain()
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    monkeypatch.setattr(vault, "backend_factory", lambda: fake)
    vault.reset()
    login()
    core.reset_runtime_state()
    run(capsys, "modules", "--json")
    assert SessionStore().load().storage == "file" and not fake.items


def test_switching_the_keychain_off_moves_the_secrets_back_into_the_file(
    gate_on, keychain, monkeypatch, login, capsys
):
    login()
    session = SessionStore().load()
    assert session.storage == "keychain"
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    vault.reset()
    code, _o, err = run(
        capsys, "login", "--email", "ada@example.test"
    )  # a fresh sign-in, file mode
    assert code == 0, err
    now = SessionStore().load()
    assert now.storage == "file" and now.refresh_token in file_text()
    assert not keychain.items, "the old keychain entry must not be left behind"


# -- failures never lose a rotated refresh token and never delete a session ---------------------


def expired_cli_session(gate_on, store_):
    token = gate_on.keys.token(iat=gate_on.now - 7200)
    store_.save(
        StoredSession(
            access_token=token,
            refresh_token="refresh-0",
            user_id="11111111-2222-3333-4444-555555555555",
            email="ada@example.test",
            source="cli",
            issued_at=gate_on.now - 7200,
            high_water=gate_on.now,
        )
    )


def test_renewal_rotates_the_tokens_inside_the_keychain(gate_on, keychain):
    s = SessionStore()
    expired_cli_session(gate_on, s)
    assert s.load().storage == "keychain"
    account = core.check_stored_session(s)
    assert not account.stale
    now = s.load()
    assert now.refresh_token == "refresh-1" and now.storage == "keychain"
    assert "refresh-1" in keychain.everything() and "refresh-1" not in file_text()
    assert "refresh-0" not in keychain.everything(), "the spent token must not linger"


def test_if_the_keychain_cannot_be_written_the_rotated_token_goes_to_the_file(gate_on, keychain):
    s = SessionStore()
    expired_cli_session(gate_on, s)
    keychain.fail = "write"  # reads work, writes are refused (a locked collection mid-run)
    vault.reset()
    vault.status()  # (the probe itself needs a write: mark the keychain usable as it was)
    # the keychain looked fine at the start of the run ...
    vault._state.update(checked=True, usable=True, reason="", backend=keychain, name="Fake")
    account = core.check_stored_session(s)
    assert not account.stale
    kept = s.load_strict()
    assert kept.refresh_token == "refresh-1", "the rotated refresh token must never be lost"
    assert kept.storage == "file" and "keychain" in kept.storage_note
    assert json.loads(file_text())["refresh_token"] == "refresh-1"


def test_a_locked_keychain_says_so_and_keeps_everything(gate_on, keychain, login, capsys):
    login()
    before = file_text()
    keychain.fail = "locked"
    vault.reset()
    core.reset_runtime_state()
    code, _o, err = run(capsys, "modules")
    assert code == 5 and "keychain" in err.lower() and "Traceback" not in err
    assert file_text() == before and keychain.items  # nothing deleted anywhere
    keychain.fail = None
    vault.reset()
    core.reset_runtime_state()
    code, _o, err = run(capsys, "modules", "--json")
    assert code == 0, err


def test_a_keychain_entry_that_vanished_means_not_signed_in(gate_on, keychain, login, capsys):
    login()
    keychain.items.clear()  # the keychain was reset
    core.reset_runtime_state()
    code, _o, err = run(capsys, "modules")
    assert code == 5 and "not signed in" in err
    code, _o, _e = run(capsys, "login", "--email", "ada@example.test")  # and signing in again works
    assert code == 0 and SessionStore().load().storage == "keychain"


def test_a_server_refusal_removes_the_keychain_entry_too(gate_on, keychain):
    s = SessionStore()
    expired_cli_session(gate_on, s)
    gate_on.mode = "rejected"
    with pytest.raises(AccountRequired) as caught:
        core.check_stored_session(s)
    assert caught.value.reason == "revoked"
    assert not s.path.exists()
    assert not [k for k in keychain.items if not k[1].startswith("probe")]


# -- sign-out -----------------------------------------------------------------------------------


def test_logout_removes_the_file_and_the_keychain_entry(gate_on, keychain, login, capsys):
    login()
    assert keychain.items
    code, out, _e = run(capsys, "logout")
    assert code == 0 and "Signed out" in out
    assert not SessionStore().path.exists() and not keychain.items


def test_logout_that_cannot_reach_the_keychain_keeps_the_file_so_it_can_be_retried(
    gate_on, keychain, login, capsys
):
    login()
    keychain.fail = "locked"
    vault.reset()
    code, _o, err = run(capsys, "logout")
    assert code != 0 and "keychain" in err.lower()
    assert SessionStore().path.exists() and keychain.items
    keychain.fail = None
    vault.reset()
    code, _o, _e = run(capsys, "logout")
    assert code == 0 and not SessionStore().path.exists() and not keychain.items


# -- tokens and the app -------------------------------------------------------------------------


def test_an_access_token_session_keeps_the_token_in_the_keychain(
    gate_on, keychain, monkeypatch, capsys
):
    token = gate_on.create_pat(name="nightly")
    monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
    code, _o, err = run(capsys, "login", "--token")
    assert code == 0, err
    assert token not in file_text() and token in keychain.everything()
    s = SessionStore().load()
    assert s.source == "pat" and s.pat_token == token and s.storage == "keychain"
    gate_on.now += 3700
    core.reset_runtime_state()
    assert core.require_account().token_name == "nightly"  # renewed through the keychain


def test_the_session_handed_over_by_the_app_goes_to_the_keychain_too(gate_on, keychain):
    core.verify_presented_token(gate_on.keys.token(), persist=True)
    s = SessionStore().load()
    assert s.source == "app" and s.storage == "keychain"
    assert s.access_token not in file_text()


def test_the_environment_token_never_touches_the_keychain(gate_on, keychain, monkeypatch):
    monkeypatch.setenv("RADARX_TOKEN", gate_on.create_pat())
    core.require_account()
    assert not keychain.items and not keychain.calls


# -- the keychain is probed, bounded and optional -----------------------------------------------


def test_the_probe_is_a_real_round_trip_and_leaves_nothing_behind(keychain):
    ok, name, reason = vault.status()
    assert ok and reason == "" and name
    assert keychain.items == {}  # the probe item was written and removed again
    assert any(c[0] == "set" for c in keychain.calls) and any(
        c[0] == "delete" for c in keychain.calls
    )


def test_no_keychain_means_the_file_and_it_says_why(gate_on, monkeypatch, login, capsys):
    monkeypatch.setenv("RADARX_SECRET_STORE", "keychain")
    monkeypatch.setattr(vault, "backend_factory", lambda: None)
    vault.reset()
    login()
    s = SessionStore().load()
    assert s.storage == "file" and "no system keychain" in s.storage_note
    _code, out, _e = run(capsys, "whoami")
    assert "private file" in out and "no system keychain" in out
    assert oct(SessionStore().path.stat().st_mode & 0o777) == "0o600"


def test_a_keychain_that_never_answers_does_not_hang_a_command(gate_on, monkeypatch, login):
    slow = FakeKeychain()
    slow.delay = 3.0
    install(monkeypatch, slow)
    monkeypatch.setattr(vault, "TIMEOUT_S", 0.3)
    started = time.monotonic()
    login()
    assert time.monotonic() - started < 3, "the login waited for the hung keychain"
    s = SessionStore().load()
    assert s.storage == "file" and "did not answer" in s.storage_note
    # and the rest of the process does not try again (no pile of hung threads)
    before = len(slow.calls)
    SessionStore().save(s)
    assert len(slow.calls) == before


def test_the_mode_variable(monkeypatch):
    monkeypatch.setenv("RADARX_SECRET_STORE", "FILE")
    assert vault.mode() == "file"
    monkeypatch.setenv("RADARX_SECRET_STORE", "keychain")  # the keychain on any system
    assert vault.mode() == "auto"
    monkeypatch.setenv("RADARX_SECRET_STORE", "whatever")  # unknown words mean the default
    default = "file" if sys.platform == "darwin" else "auto"
    assert vault.mode() == default
    monkeypatch.delenv("RADARX_SECRET_STORE")
    assert vault.mode() == default
    monkeypatch.setenv("RADARX_SECRET_STORE", "file")
    ok, _name, reason = vault.status()
    assert not ok and "RADARX_SECRET_STORE=file" in reason


def test_a_plug_in_backend_is_never_chosen(monkeypatch):
    """The backend comes from the platform, not from keyring's discovery, so keyrings.alt (plain
    files) or a user-installed backend cannot take over."""
    import inspect

    source = inspect.getsource(vault._make_backend)
    assert "get_keyring" not in source and "set_keyring" not in source
    assert "entry_points" not in source


def test_a_damaged_header_is_reported_not_trusted(keychain):
    keychain.items[("radarx-desktop", "acct")] = "not json"
    with pytest.raises(vault.VaultError):
        vault.read("acct")
    keychain.items[("radarx-desktop", "acct")] = json.dumps({"v": 1, "id": "x", "n": 9999})
    with pytest.raises(vault.VaultError):
        vault.read("acct")
    keychain.items[("radarx-desktop", "acct")] = json.dumps({"v": 1, "id": "x", "n": 2})
    with pytest.raises(vault.VaultError, match="incomplete"):
        vault.read("acct")


def test_a_rewrite_never_leaves_a_reader_with_a_mix_of_versions(keychain):
    vault.write("acct", "a" * 2000)
    vault.write("acct", "b" * 2500)
    assert vault.read("acct") == "b" * 2500
    names = [k[1] for k in keychain.items if k[1].startswith("acct#")]
    assert len(names) == 4, "the old version's chunks were removed"
    vault.delete("acct")
    assert vault.read("acct") is None and not keychain.items
    vault.delete("acct")  # nothing to delete is not an error


def test_the_value_is_never_in_an_error_message(gate_on, keychain, login):
    login()
    secret = SessionStore().load().refresh_token
    keychain.fail = "locked"
    vault.reset()
    with pytest.raises(vault.VaultError) as caught:
        vault.read("whatever")
    assert secret not in str(caught.value)


def test_old_programs_reading_the_new_file_just_see_no_session(gate_on, keychain, login):
    """A previous version has no idea of the marker: the file has no access_token, which it reads as
    'not signed in' (never a crash, never a token)."""
    login()
    data = json.loads(file_text())
    assert StoredSession.from_json(data) is None
    assert store.StoredSession.from_json({**data, "access_token": "x"}) is not None


# -- the real keychain of this computer (opt in: it writes and removes items of its own) --------


@pytest.mark.skipif(
    os.environ.get("RADARX_TEST_REAL_KEYCHAIN") != "1",
    reason="set RADARX_TEST_REAL_KEYCHAIN=1 to try this computer's real keychain",
)
def test_the_real_keychain_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("RADARX_SECRET_STORE", "auto")
    monkeypatch.setattr(vault, "backend_factory", vault._make_backend)
    vault.reset()
    usable, _name, reason = vault.status()
    if not usable:
        pytest.skip(f"no usable keychain here: {reason}")
    s = SessionStore(tmp_path / "cfg")
    big = "e" * 2500  # longer than one Windows Credential Manager item
    session = StoredSession(
        access_token=big,
        refresh_token="refresh-real-1",
        user_id="u",
        email="ada@example.test",
        source="cli",
        issued_at=time.time(),
        high_water=time.time(),
    )
    try:
        s.save(session)
        assert s.load_strict().storage == "keychain"
        back = s.load_strict()
        assert back.access_token == big and back.refresh_token == "refresh-real-1"
        text = s.path.read_text()
        assert big not in text and "refresh-real-1" not in text
        s.clear()
        assert not s.path.exists() and vault.read(s.account) is None
    finally:
        with contextlib.suppress(Exception):
            vault.delete(s.account)


def test_the_timeout_message_on_a_mac_names_the_permission_prompt(monkeypatch):
    slow = FakeKeychain()
    slow.delay = 2.0
    install(monkeypatch, slow)
    monkeypatch.setattr(vault, "TIMEOUT_S", 0.2)
    monkeypatch.setattr("sys.platform", "darwin")
    ok, _name, reason = vault.status()
    assert not ok and "Always Allow" in reason and "RADARX_SECRET_STORE=file" in reason
    vault.reset()
    monkeypatch.setattr("sys.platform", "linux")
    ok, _name, reason = vault.status()
    assert not ok and "Always Allow" not in reason and "did not answer" in reason


def test_recording_activity_and_noting_the_time_do_not_read_the_keychain(gate_on, keychain, login):
    from engine.account import activity

    login()
    keychain.calls.clear()
    for _ in range(5):
        activity.record("command", surface="cli", command="info")
    SessionStore().note_time(time.time())
    assert activity.queued_count() >= 5
    assert not [c for c in keychain.calls if c[0] == "get"], "the user id is not a secret"
