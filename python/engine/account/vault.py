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


def explicit_file() -> bool:
    """The user chose the file (``RADARX_SECRET_STORE=file``): the keychain is never touched."""
    return os.environ.get(MODE_VARIABLE, "").strip().lower() == "file"


def mode() -> str:
    """Where NEW secrets may go: ``"auto"`` (the keychain, when there is a working one) or
    ``"file"``. ``RADARX_SECRET_STORE`` is ``file``, ``keychain`` (use it on any system) or
    ``auto``/unset: the platform default, which is the keychain on Windows and Linux and the
    private FILE on macOS, because on macOS a second program (the app's engine and ``rxd``) that
    reads an item the first one wrote raises a permission prompt that a short-lived command
    cannot answer. A session that already lives in the keychain stays readable and writable
    there unless the file was chosen explicitly (see :func:`status`, ``force``)."""
    word = os.environ.get(MODE_VARIABLE, "").strip().lower()
    if word == "file":
        return "file"
    if word == "keychain":
        return "auto"
    return "file" if sys.platform == "darwin" else "auto"


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


def status(force: bool = False) -> tuple[bool, str, str]:
    """``(usable, backend name, reason)``. ``reason`` explains why the keychain is not used.
    ``force`` looks past the macOS default (never past an explicit ``file`` choice): it is for a
    session that already lives in the keychain."""
    if explicit_file():
        return False, "", f"by your choice ({MODE_VARIABLE}=file)"
    if mode() == "file" and not force:
        return (
            False,
            "",
            f"macOS keeps the sign-in in the private file by default ({MODE_VARIABLE}=keychain "
            "uses the Keychain)",
        )
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


def _backend(*, force: bool = False, ignore_choice: bool = False) -> Any:
    """The working backend. ``force``: look past the macOS default (an entry that already exists
    must be reachable). ``ignore_choice`` (only to remove an old entry after the file was chosen)
    skips the explicit-file check and the probe as well."""
    if ignore_choice:
        backend = backend_factory()
        if backend is None:
            raise VaultError("no system keychain is available")
        return backend
    usable, _name, reason = status(force=force)
    if not usable:
        raise VaultError(reason or "no system keychain is available")
    with _lock:
        return _state["backend"]


# -- values of any size, in chunks -------------------------------------------------------------


def _header(version: str, count: int, stale: list[dict[str, Any]] | None = None) -> str:
    head: dict[str, Any] = {"v": 1, "id": version, "n": count}
    if stale:
        head["stale"] = stale[:8]  # old versions whose parts could not be removed yet
    return json.dumps(head, separators=(",", ":"))


def _check_part(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("n"), int)
        and 1 <= item["n"] <= 64
    )


def _read_header(backend: Any, account: str) -> dict[str, Any] | None:
    raw = _call("read", backend.get_password, SERVICE, account)
    if raw is None:
        return None
    try:
        head = json.loads(raw)
    except ValueError:
        raise VaultError("the keychain entry is not one of ours") from None
    if not _check_part(head):
        raise VaultError("the keychain entry is not one of ours")
    stale = head.get("stale")
    head["stale"] = [x for x in stale if _check_part(x)] if isinstance(stale, list) else []
    return head


def _chunk_name(account: str, version: str, index: int) -> str:
    return f"{account}#{version}#{index}"


def write(account: str, text: str, *, force: bool = False) -> dict[str, Any]:
    """Store ``text`` under ``account`` and return a handle for :func:`discard`. The previous value
    stays readable until the new one is complete (new chunks first, then the header that points at
    them, then the old chunks go). Old parts that cannot be removed are listed in the new header,
    so a later write or :func:`delete` removes them (they hold secrets)."""
    backend = _backend(force=force)
    try:
        old = _read_header(backend, account)
    except VaultError:
        old = None
    version = secrets.token_hex(6)
    chunks = [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)] or [""]
    for i, chunk in enumerate(chunks):
        _call("write", backend.set_password, SERVICE, _chunk_name(account, version, i), chunk)
    stale: list[dict[str, Any]] = []
    if old is not None:
        for part in [{"id": old["id"], "n": old["n"]}, *old["stale"]]:
            if _drop_chunks(backend, account, part):
                stale.append(part)
    _call("write", backend.set_password, SERVICE, account, _header(version, len(chunks), stale))
    return {"v": 1, "id": version, "n": len(chunks)}


def discard(account: str, handle: dict[str, Any]) -> None:
    """Remove what a :func:`write` stored, by its handle (for a copy that could not be verified,
    when reading the header back is the very thing that failed). Never raises."""
    with contextlib.suppress(VaultError):
        backend = _backend(force=True)
        _drop_chunks(backend, account, handle)
        _call("delete", backend.delete_password, SERVICE, account)


def _gone(backend: Any, name: str) -> bool:
    try:
        return _call("check", backend.get_password, SERVICE, name) is None
    except VaultError:
        return False


def _drop_chunks(backend: Any, account: str, part: dict[str, Any]) -> int:
    """Delete the parts of one version, retrying each; returns how many are still there."""
    left = 0
    for i in range(part["n"]):
        name = _chunk_name(account, part["id"], i)
        for attempt in range(3):
            try:
                _call("delete", backend.delete_password, SERVICE, name)
                break
            except VaultError:
                if _gone(backend, name):  # "no such item": already removed
                    break
                time.sleep(0.05 * (attempt + 1))
        else:
            left += 1
    return left


def read(account: str) -> str | None:
    """The value stored under ``account``, or None when there is none. Raises :class:`VaultError`
    when the keychain cannot be used or the entry is damaged."""
    backend = _backend(force=True)
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


def delete(account: str, *, ignore_choice: bool = False) -> None:
    """Remove the value (and its chunks). Nothing to remove is not an error; parts that cannot be
    removed raise :class:`VaultError` and leave the header, so removing can be tried again."""
    backend = _backend(force=True, ignore_choice=ignore_choice)
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
    left = _drop_chunks(backend, account, head)
    for part in head["stale"]:
        left += _drop_chunks(backend, account, part)
    if left:
        raise VaultError(
            f"{left} part(s) of the saved sign-in could not be removed from the keychain"
        )
    try:
        _call("delete", backend.delete_password, SERVICE, account)
    except VaultError as exc:
        if _read_header(backend, account) is not None:  # really still there
            raise exc
