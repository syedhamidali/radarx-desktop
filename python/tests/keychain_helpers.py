"""Test doubles for the operating system's keychain (engine/account/vault.py). Nothing here touches
a real keychain: the keyring backend is replaced by an in-memory (or file-backed) fake."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any


class FakeKeychain:
    """The three calls of a keyring backend, with the failure modes of real ones."""

    def __init__(self, *, max_chars: int | None = None) -> None:
        self.items: dict[tuple[str, str], str] = {}
        #: Windows Credential Manager holds about 1280 characters per item: set to test chunking
        self.max_chars = max_chars
        #: None, or "locked" (every call fails) or "write" (only writes fail)
        self.fail: str | None = None
        self.delay = 0.0
        #: when True, everything but the probe's own item reads back as missing (a keychain that
        #: accepts a write and then does not return it)
        self.forget = False
        self.calls: list[tuple[str, str]] = []

    def _check(self, what: str, write: bool) -> None:
        time.sleep(self.delay)
        if self.fail == "locked" or (self.fail == "write" and write):
            raise RuntimeError(f"keychain {what} refused")

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", username))
        self._check("write", True)
        if self.max_chars is not None and len(password) > self.max_chars:
            raise RuntimeError("item too large")
        self.items[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append(("get", username))
        self._check("read", False)
        if self.forget and not username.startswith("probe-"):
            return None
        return self.items.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", username))
        self._check("delete", True)
        if (service, username) not in self.items:
            raise RuntimeError("no such item")
        del self.items[(service, username)]

    def everything(self) -> str:
        """All stored values joined: for "the secret is in the keychain / not in the file" checks."""
        return "\n".join(self.items.values())


class FileKeychain:
    """A fake keychain shared by several PROCESSES: one JSON file, updated under an exclusive
    lock (what a real keychain's own serialisation gives)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextlib.contextmanager
    def _locked(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._locked():
            data = self._load()
            data[f"{service}/{username}"] = password
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)

    def get_password(self, service: str, username: str) -> str | None:
        with self._locked():
            return self._load().get(f"{service}/{username}")

    def delete_password(self, service: str, username: str) -> None:
        with self._locked():
            data = self._load()
            if f"{service}/{username}" not in data:
                raise RuntimeError("no such item")
            del data[f"{service}/{username}"]
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)


def install(monkeypatch: Any, fake: Any) -> None:
    """Make ``engine.account.vault`` use ``fake`` as the system keychain."""
    from engine.account import vault

    monkeypatch.setenv("RADARX_SECRET_STORE", "auto")
    monkeypatch.setattr(vault, "backend_factory", lambda: fake)
    vault.reset()
