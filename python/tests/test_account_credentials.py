"""Sign-in tokens in the system credential store, with a private file as the fallback.

The logic runs against an in-memory credential backend (nothing touches the real store of the
computer running the tests). Two checks run against the real thing and are skipped when it is not
there: the Windows access list of the fallback file, and a round trip through the operating
system's own store (set RADARX_TEST_REAL_KEYRING=1 to run it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

keyring = pytest.importorskip("keyring")
from keyring.backend import KeyringBackend

from engine.account import credentials
from engine.account.store import SessionStore, StoredSession, write_private

ACCESS = "eyJhbGciOiJFUzI1NiJ9." + "a" * 1100 + ".sig"  # about the length of a real token
REFRESH = "refresh-" + "r" * 60


class MemoryKeyring(KeyringBackend):
    """A credential store held in a dict, with a limit on the size of one value like Windows."""

    priority = 1.0  # type: ignore[assignment]

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail_on: str | None = None  # "get" | "set"
        self.max_chars = 1280

    def get_password(self, service, username):
        if self.fail_on == "get":
            raise RuntimeError("store locked")
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        if self.fail_on == "set":
            raise RuntimeError("store locked")
        assert len(password) <= self.max_chars, "value too long for the store"
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture
def backend(monkeypatch):
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "keyring")
    yield memory
    keyring.set_keyring(previous)


def session(access: str = ACCESS, refresh: str | None = REFRESH) -> StoredSession:
    return StoredSession(
        access_token=access,
        user_id="user-1",
        email="ada@example.org",
        source="cli",
        refresh_token=refresh,
        issued_at=1000.0,
        high_water=2000.0,
    )


def test_tokens_go_to_the_store_and_not_into_the_file(backend, tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    text = store.path.read_text(encoding="utf-8")
    assert ACCESS not in text and REFRESH not in text
    data = json.loads(text)
    assert data["credential_store"] == "keyring"
    assert "access_token" not in data and "refresh_token" not in data
    assert data["email"] == "ada@example.org" and data["issued_at"] == 1000.0
    loaded = SessionStore(tmp_path).load()
    assert loaded is not None
    assert loaded.access_token == ACCESS and loaded.refresh_token == REFRESH
    assert loaded.high_water == 2000.0 and loaded.source == "cli"
    assert backend.items and all(
        len(v) <= 400 for k, v in backend.items.items() if k[1] != "session.n"
    )


def test_a_longer_token_than_one_entry_holds_round_trips(backend, tmp_path):
    long_token = "t" * 3000
    SessionStore(tmp_path).save(session(access=long_token))
    assert SessionStore(tmp_path).load().access_token == long_token


def test_a_shorter_value_leaves_no_old_chunks_behind(backend, tmp_path):
    store = SessionStore(tmp_path)
    store.save(session(access="t" * 3000))
    many = len(backend.items)
    store.save(session(access="short", refresh=None))
    assert len(backend.items) < many
    assert store.load().access_token == "short"


def test_a_plain_file_from_an_earlier_version_moves_into_the_store(monkeypatch, backend, tmp_path):
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "file")
    SessionStore(tmp_path).save(session())
    assert ACCESS in (tmp_path / "session.json").read_text(encoding="utf-8")
    assert not backend.items
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "keyring")
    first = SessionStore(tmp_path).load()
    assert first is not None and first.access_token == ACCESS  # nobody is signed out by it
    text = (tmp_path / "session.json").read_text(encoding="utf-8")
    assert ACCESS not in text and REFRESH not in text
    assert backend.items
    again = SessionStore(tmp_path).load()
    assert again is not None and again.refresh_token == REFRESH


def test_when_the_store_refuses_the_file_keeps_the_session(backend, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "file")
    SessionStore(tmp_path).save(session())
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "keyring")
    backend.fail_on = "set"
    store = SessionStore(tmp_path)
    loaded = store.load()
    assert loaded is not None and loaded.access_token == ACCESS
    assert ACCESS in (tmp_path / "session.json").read_text(encoding="utf-8")  # still there
    # a new save while the store is failing falls back to the file, and says so once
    with caplog.at_level("WARNING", logger="radarx.account"):
        store.save(session(access="new-token"))
        store.save(session(access="newer-token"))
    assert store.load().access_token == "newer-token"
    assert sum("stays in its private file" in r.getMessage() for r in caplog.records) == 1


def test_a_store_that_cannot_be_read_is_not_mistaken_for_signed_out(backend, tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    backend.fail_on = "get"
    with pytest.raises(OSError):
        store.load_strict()
    assert store.load() is None  # load() never raises
    backend.fail_on = None
    assert store.load().access_token == ACCESS  # and nothing was lost


def test_a_file_whose_secrets_are_gone_is_signed_out(backend, tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    backend.items.clear()
    assert store.load() is None


def test_signing_out_removes_the_file_and_the_secrets(backend, tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    store.clear()
    assert not store.path.exists() and not backend.items


def test_update_under_the_lock_works_with_the_store(backend, tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())

    def newer(current):
        assert current is not None and current.access_token == ACCESS
        current.access_token = "renewed"
        return current

    store.update(newer)
    assert SessionStore(tmp_path).load().access_token == "renewed"
    assert "renewed" not in store.path.read_text(encoding="utf-8")


def test_file_mode_never_touches_the_store(monkeypatch, backend, tmp_path):
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "file")
    store = SessionStore(tmp_path)
    store.save(session())
    store.clear()
    assert not backend.items


def test_a_folder_chosen_by_the_caller_does_not_use_the_store_in_auto_mode(
    monkeypatch, backend, tmp_path
):
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "auto")
    store = SessionStore(tmp_path)
    store.save(session())
    assert ACCESS in store.path.read_text(encoding="utf-8")
    assert not backend.items


def test_no_backend_means_the_private_file(monkeypatch, tmp_path):
    from keyring.backends import fail

    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "keyring")
    try:
        store = SessionStore(tmp_path)
        store.save(session())
        assert ACCESS in store.path.read_text(encoding="utf-8")
        assert store.load().access_token == ACCESS
    finally:
        keyring.set_keyring(previous)


def test_secrets_are_not_in_a_log_line(backend, tmp_path, caplog):
    caplog.set_level("DEBUG")
    backend.fail_on = "set"
    store = SessionStore(tmp_path)
    store.save(session())
    store.load()
    for secret in (ACCESS, REFRESH):
        assert secret not in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows access lists")
def test_on_windows_the_private_file_names_only_the_current_user(tmp_path):
    path = tmp_path / "private.json"
    write_private(path, '{"secret": 1}', secret=True)
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
    entries = [line.strip() for line in out.splitlines() if ":(" in line]
    assert len(entries) == 1, out
    assert os.environ["USERNAME"].lower() in entries[0].lower()
    assert path.read_text(encoding="utf-8") == '{"secret": 1}'


@pytest.mark.skipif(
    os.environ.get("RADARX_TEST_REAL_KEYRING") != "1",
    reason="uses this computer's real credential store; set RADARX_TEST_REAL_KEYRING=1",
)
def test_round_trip_through_the_operating_systems_own_store(monkeypatch, tmp_path):
    service = "radarx-desktop-test-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(credentials, "SERVICE", service)
    monkeypatch.setenv("RADARX_CREDENTIAL_STORE", "keyring")
    store = SessionStore(tmp_path)
    try:
        store.save(session())
        assert ACCESS not in store.path.read_text(encoding="utf-8")
        loaded = SessionStore(tmp_path).load()
        assert loaded is not None
        assert loaded.access_token == ACCESS and loaded.refresh_token == REFRESH
    finally:
        store.clear()
    assert credentials.read_secrets() is None
    assert sys.platform  # the backend in use is named in the test report below
    print("backend:", type(keyring.get_keyring()).__name__)
