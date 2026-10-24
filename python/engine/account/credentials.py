"""Where the sign-in tokens are kept: the operating system's credential store, or a private file.

The session file (``store.py``) is plain text. Where the system has a credential store, the two
secrets in it (the access token and, for ``rxd login``, the refresh token) go there instead:

* Windows  Credential Manager
* macOS    Keychain
* Linux    Secret Service (GNOME Keyring, KWallet), when a desktop session is running

through the optional ``keyring`` package (extra ``keyring``). The session file then keeps only the
non-secret fields (e-mail, who owns the refresh chain, the times of the offline rule) and the mark
``"credential_store": "keyring"``. When there is no credential store (no package, no backend, a
server or cluster without a desktop session) the tokens stay in the file, which is then readable
by the owner only: mode 0600, and on Windows an access list that names only the current user.

``RADARX_CREDENTIAL_STORE`` chooses: ``auto`` (the default), ``file`` (never use the store) or
``keyring`` (insist on it; tests use this with a fake backend). ``auto`` uses the store only for
the real per-user folder, never for a folder named with ``RADARX_CONFIG_DIR`` (tests, containers).

A credential entry is small (Windows rejects a value of more than about 1,280 characters, and a
sign-in token is longer than that with a refresh token), so the secrets are stored as JSON cut into
chunks of 400 characters, ``session.0``, ``session.1`` ... and a count under ``session.n``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("radarx.account")

SERVICE = "radarx-desktop"
MARK = "credential_store"
MARK_VALUE = "keyring"
#: The fields of a session that are secrets (everything else stays in the file).
SECRET_FIELDS = ("access_token", "refresh_token")
CHUNK_CHARS = 400
_COUNT = "session.n"
#: More chunks than any real token needs: a corrupt count cannot make us loop for long.
_MAX_CHUNKS = 50


class CredentialStoreError(OSError):
    """The credential store failed (locked, denied, no backend) while it was being used."""


def mode() -> str:
    value = os.environ.get("RADARX_CREDENTIAL_STORE", "auto").strip().lower()
    return value if value in ("auto", "file", "keyring") else "auto"


def _backend() -> Any | None:
    """The ``keyring`` module with a working backend, or None (never raises)."""
    if mode() != "keyring" and not _desktop_session_present():
        return None
    try:
        import keyring
        from keyring.backends import fail

        current = keyring.get_keyring()
        if isinstance(current, fail.Keyring):
            return None
        _ = current.priority  # a backend that cannot work raises here
        return keyring
    except Exception:
        return None


def _desktop_session_present() -> bool:
    """Linux reaches the Secret Service over the session bus; without one (ssh, a cluster login
    node, a container) it cannot answer, and waiting for it would hang the sign-in."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    return True


def wanted(default_location: bool) -> bool:
    """Should the tokens of a store at this location go to the credential store?"""
    chosen = mode()
    if chosen == "file":
        return False
    if chosen == "auto" and not default_location:
        return False
    return _backend() is not None


def _entry(index: int | str) -> str:
    return _COUNT if index == "n" else f"session.{index}"


def read_secrets() -> dict[str, str] | None:
    """The stored secrets, or None when there are none. A failing store raises
    :class:`CredentialStoreError` (so it does not look like "not signed in")."""
    keyring = _backend()
    if keyring is None:
        raise CredentialStoreError("the system credential store is not available")
    try:
        count_text = keyring.get_password(SERVICE, _COUNT)
        if not count_text:
            return None
        count = int(count_text)
        if not 1 <= count <= _MAX_CHUNKS:
            return None
        parts = [keyring.get_password(SERVICE, _entry(i)) for i in range(count)]
    except ValueError:
        return None
    except Exception as exc:
        raise CredentialStoreError(f"{type(exc).__name__} reading the credential store") from None
    if any(p is None for p in parts):
        return None
    try:
        data = json.loads("".join(parts))  # type: ignore[arg-type]
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k in SECRET_FIELDS and isinstance(v, str) and v}


def write_secrets(secrets: dict[str, str | None]) -> None:
    """Replace the stored secrets. Raises :class:`CredentialStoreError` when that did not work."""
    keyring = _backend()
    if keyring is None:
        raise CredentialStoreError("the system credential store is not available")
    text = json.dumps({k: v for k, v in secrets.items() if k in SECRET_FIELDS and v})
    chunks = [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    try:
        old = keyring.get_password(SERVICE, _COUNT)
        for i, chunk in enumerate(chunks):
            keyring.set_password(SERVICE, _entry(i), chunk)
        keyring.set_password(SERVICE, _COUNT, str(len(chunks)))
        if old and old.isdigit():  # a shorter value than before: drop the chunks left over
            for i in range(len(chunks), min(int(old), _MAX_CHUNKS)):
                with contextlib.suppress(Exception):
                    keyring.delete_password(SERVICE, _entry(i))
    except Exception as exc:
        raise CredentialStoreError(f"{type(exc).__name__} writing the credential store") from None


def delete_secrets() -> None:
    """Remove the stored secrets (best effort; nothing to remove is fine)."""
    keyring = _backend()
    if keyring is None:
        return
    with contextlib.suppress(Exception):
        count_text = keyring.get_password(SERVICE, _COUNT)
        count = int(count_text) if count_text and count_text.isdigit() else 0
        for i in range(min(count, _MAX_CHUNKS)):
            with contextlib.suppress(Exception):
                keyring.delete_password(SERVICE, _entry(i))
        with contextlib.suppress(Exception):
            keyring.delete_password(SERVICE, _COUNT)


def restrict_to_owner(path: str | Path) -> bool:
    """Make ``path`` readable by the current user only. Returns whether that was done.

    POSIX: mode 0600. Windows: the inherited rights are removed and only the current user is
    named (``icacls``, part of Windows), so other accounts on the computer cannot read it.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False
    user = os.environ.get("USERNAME")
    if not user:
        return False
    domain = os.environ.get("USERDOMAIN")
    who = f"{domain}\\{user}" if domain else user
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
