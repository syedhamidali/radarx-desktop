"""The operating system's keychain for the secrets of a session (docs/ACCOUNTS.md).

macOS Keychain, Windows Credential Manager and the Linux Secret Service (GNOME Keyring, KWallet)
hold the tokens instead of a file, through the ``keyring`` package. Where there is no keychain
(a headless Linux box, a container, an HPC node: no D-Bus session, no unlocked collection) every
call here fails fast and the caller keeps the private session file (mode 0600), which stays fully
supported.

Rules this module keeps:

* The backend is chosen by platform, **explicitly** (no entry-point discovery, so it also works in
  the PyInstaller bundle and a plug-in backend such as ``keyrings.alt`` can never be picked up:
  that one stores secrets in a plain file).
* Whether a keychain works is decided by a real round trip (write, read back, delete), once per
  process, with a time limit: a locked collection waiting for a password prompt must not hang a
  command. Every later call has the same time limit.
* One logical value is split into chunks of 800 characters (Windows Credential Manager stores at
  most 2560 bytes per item, a Supabase access token can be longer than that). The header item
  names the version of the chunks it belongs to, so a reader never mixes two versions.
* Nothing here logs or prints a value. Errors name the backend and the failing step, never the
  data.
* ``RADARX_SECRET_STORE=file`` switches the keychain off (containers, people who prefer the file).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import threading
import time
from typing import Any

SERVICE = "radarx-desktop"
CHUNK_CHARS = 800
TIMEOUT_S = 5.0
MODE_VARIABLE = "RADARX_SECRET_STORE"


class VaultError(OSError):
    """The keychain could not be used (missing, locked, refused, too slow); safe to show."""


_lock = threading.Lock()
_state: dict[str, Any] = {
    "checked": False,
    "usable": False,
    "reason": "",
    "backend": None,
    "name": "",
}


def mode() -> str:
    """``"auto"`` (use the keychain when there is one) or ``"file"`` (never)."""
    return "file" if os.environ.get(MODE_VARIABLE, "").strip().lower() == "file" else "auto"


#: What went wrong importing the backend of this platform, for :func:`diagnose` (a packaged build
#: that forgot to bundle it must be told apart from a computer that simply has no keychain).
_import_problem: dict[str, str] = {"error": ""}


def _make_backend() -> Any | None:
    """The keyring backend of this platform, or None. Chosen by platform, never by discovery."""
    _import_problem["error"] = ""
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring as MacOS

            return MacOS()
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            return WinVaultKeyring()
        if sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd")):
            from keyring.backends.SecretService import Keyring as SecretService

            return SecretService()
    except Exception as exc:  # not installed, or no D-Bus library: no keychain here
        _import_problem["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return None
    return None


def backend_label(backend: Any) -> str:
    module = type(backend).__module__
    if module.endswith("macOS"):
        return "macOS Keychain"
    if module.endswith("Windows"):
        return "Windows Credential Manager"
    if module.endswith("SecretService"):
        return "Secret Service (GNOME Keyring or KWallet)"
    return type(backend).__name__


#: Replaced by tests to supply a fake keychain; production code never changes it.
backend_factory = _make_backend


def reset() -> None:
    """Forget the probe result (tests; a new process always probes again)."""
    with _lock:
        _state.update(checked=False, usable=False, reason="", backend=None, name="")


def _call(label: str, fn: Any, *args: Any) -> Any:
    """Run one keychain call with a time limit. A call that never returns (a locked collection
    waiting for a prompt nobody sees) leaves a daemon thread behind and turns the keychain off for
    the rest of this process."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # handed to the caller below
            box["error"] = exc

    worker = threading.Thread(target=work, name="radarx-keychain", daemon=True)
    worker.start()
    worker.join(TIMEOUT_S)
    if worker.is_alive():
        with _lock:
            _state.update(usable=False, reason="the keychain did not answer in time")
        hint = (
            "; macOS may be asking for permission: look for a dialog and choose Always Allow, "
            f"or set {MODE_VARIABLE}=file"
            if sys.platform == "darwin"
            else ""
        )
        raise VaultError(f"the keychain did not answer within {TIMEOUT_S:g} s ({label}){hint}")
    if "error" in box:
        exc = box["error"]
        if isinstance(exc, VaultError):
            raise exc
        raise VaultError(
            f"the keychain refused the request ({label}: {type(exc).__name__})"
        ) from None
    return box.get("value")


def _probe(backend: Any) -> None:
    """A real round trip with a throw-away item."""
    name = f"probe-{secrets.token_hex(6)}"
    value = secrets.token_hex(16)
    _call("probe write", backend.set_password, SERVICE, name, value)
    try:
        if _call("probe read", backend.get_password, SERVICE, name) != value:
            raise VaultError("the keychain did not return what was stored")
    finally:
        with contextlib.suppress(VaultError):
            _call("probe delete", backend.delete_password, SERVICE, name)


def status() -> tuple[bool, str, str]:
    """``(usable, backend name, reason)``. ``reason`` explains why the keychain is not used."""
    if mode() == "file":
        return False, "", f"by your choice ({MODE_VARIABLE}=file)"
    with _lock:
        if _state["checked"]:
            return bool(_state["usable"]), str(_state["name"]), str(_state["reason"])
    backend = backend_factory()
    if backend is None:
        result = (False, "", "no system keychain is available on this computer")
        with _lock:
            _state.update(checked=True, usable=False, reason=result[2], backend=None, name="")
        return result
    name = backend_label(backend)
    try:
        _probe(backend)
    except VaultError as exc:
        with _lock:
            _state.update(
                checked=True, usable=False, reason=f"{name}: {exc}", backend=None, name=name
            )
        return False, name, f"{name}: {exc}"
    with _lock:
        _state.update(checked=True, usable=True, reason="", backend=backend, name=name)
    return True, name, ""


def diagnose() -> dict[str, Any]:
    """What ``xradar-engine --keychain-check`` prints: does the keychain work here, and if not why.
    ``import_error`` is set when the keyring backend could not even be imported (a packaging
    problem, not the absence of a keychain)."""
    usable, name, reason = status()
    return {
        "usable": usable,
        "backend": name,
        "reason": reason,
        "import_error": _import_problem["error"],
        "mode": mode(),
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def available() -> bool:
    return status()[0]


def _backend(*, even_if_off: bool = False) -> Any:
    """The working backend. ``even_if_off`` (only for removing an old entry after the keychain was
    switched off by choice) skips the mode check and the probe."""
    if even_if_off and mode() == "file":
        backend = backend_factory()
        if backend is None:
            raise VaultError("no system keychain is available")
        return backend
    usable, _name, reason = status()
    if not usable:
        raise VaultError(reason or "no system keychain is available")
    with _lock:
        return _state["backend"]


# -- values of any size, in chunks -------------------------------------------------------------


def _header(version: str, count: int) -> str:
    return json.dumps({"v": 1, "id": version, "n": count}, separators=(",", ":"))


def _read_header(backend: Any, account: str) -> dict[str, Any] | None:
    raw = _call("read", backend.get_password, SERVICE, account)
    if raw is None:
        return None
    try:
        head = json.loads(raw)
    except ValueError:
        raise VaultError("the keychain entry is not one of ours") from None
    ok = (
        isinstance(head, dict)
        and isinstance(head.get("id"), str)
        and isinstance(head.get("n"), int)
    )
    if not ok or not 1 <= head["n"] <= 64:
        raise VaultError("the keychain entry is not one of ours")
    return head


def _chunk_name(account: str, version: str, index: int) -> str:
    return f"{account}#{version}#{index}"


def write(account: str, text: str) -> dict[str, Any]:
    """Store ``text`` under ``account`` and return a handle for :func:`discard`. The previous value
    stays readable until the new one is complete (new chunks first, then the header that points at
    them, then the old chunks go)."""
    backend = _backend()
    try:
        old = _read_header(backend, account)
    except VaultError:
        old = None
    version = secrets.token_hex(6)
    chunks = [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or [""]
    for i, chunk in enumerate(chunks):
        _call("write", backend.set_password, SERVICE, _chunk_name(account, version, i), chunk)
    _call("write", backend.set_password, SERVICE, account, _header(version, len(chunks)))
    if old is not None:
        _drop_chunks(backend, account, old)
    return {"v": 1, "id": version, "n": len(chunks)}


def discard(account: str, handle: dict[str, Any]) -> None:
    """Remove what a :func:`write` stored, by its handle (for a copy that could not be verified,
    when reading the header back is the very thing that failed). Never raises."""
    with contextlib.suppress(VaultError):
        backend = _backend()
        _drop_chunks(backend, account, handle)
        _call("delete", backend.delete_password, SERVICE, account)


def _drop_chunks(backend: Any, account: str, head: dict[str, Any]) -> None:
    for i in range(head["n"]):
        # an orphan chunk is harmless and unreadable without its header
        with contextlib.suppress(VaultError):
            _call("delete", backend.delete_password, SERVICE, _chunk_name(account, head["id"], i))


def read(account: str) -> str | None:
    """The value stored under ``account``, or None when there is none. Raises :class:`VaultError`
    when the keychain cannot be used or the entry is damaged."""
    backend = _backend()
    for attempt in (0, 1):
        head = _read_header(backend, account)
        if head is None:
            return None
        parts: list[str] = []
        for i in range(head["n"]):
            piece = _call(
                "read", backend.get_password, SERVICE, _chunk_name(account, head["id"], i)
            )
            if piece is None:
                break  # a writer replaced the version between our two reads: look again
            parts.append(piece)
        else:
            return "".join(parts)
        time.sleep(0.05 * (attempt + 1))
    raise VaultError("the keychain entry is incomplete")


def delete(account: str, *, even_if_off: bool = False) -> None:
    """Remove the value (and its chunks). Nothing to remove is not an error."""
    backend = _backend(even_if_off=even_if_off)
    try:
        head = _read_header(backend, account)
    except VaultError as exc:
        if "not one of ours" not in str(exc):
            raise
        head = None  # a damaged header: remove it below, there are no chunks to find
    if head is None:
        with contextlib.suppress(VaultError):
            _call("delete", backend.delete_password, SERVICE, account)
        return
    _drop_chunks(backend, account, head)
    try:
        _call("delete", backend.delete_password, SERVICE, account)
    except VaultError as exc:
        if _read_header(backend, account) is not None:  # really still there
            raise exc
