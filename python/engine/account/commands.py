"""The ``rxd login``, ``rxd logout`` and ``rxd whoami`` commands (registered by :mod:`engine.cli`).

The password is read with :func:`getpass.getpass` (never echoed, never on the command line, never
stored: only the session the server returns is kept, in the shared per-user session file).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from typing import Any

from engine.account import activity, config, core, gotrue
from engine.account.errors import AccountRequired, AccountServerError

#: ``engine.cli.EXIT_ACCOUNT`` (cli imports this module, so the value is repeated here).
EXIT_ACCOUNT = 5

EXAMPLES_LOGIN = (
    "examples:\n"
    "  rxd login\n"
    "  rxd login --email you@example.org\n"
    "\n"
    "Signs in with your radarx account (the same e-mail and password as in the app; create the\n"
    "account in the app or at the sign-up screen). The password is asked for without echo and is\n"
    "never stored; the session is kept in a private file in your user configuration folder and\n"
    "shared with the app and with Python (import rxd). 'rxd whoami' shows it and\n"
    "'rxd logout' removes it."
)
EXAMPLES_LOGOUT = (
    "examples:\n  rxd logout\n\n"
    "Ends the session on the server and removes the saved session from this computer."
)
EXAMPLES_WHOAMI = (
    "examples:\n  rxd whoami\n  rxd whoami --json\n\n"
    "Shows who is signed in and for how long the sign-in is good. Exit code 5 when nobody is."
)

#: Commands that work without an account (everything else needs one; see docs/ACCOUNTS.md).
EXEMPT = frozenset({"login", "logout", "whoami", "completion", "examples"})


def register(command: Any, cli: Any) -> None:
    p = command("login", "Sign in with your radarx account (e-mail and password).", EXAMPLES_LOGIN)
    p.add_argument("--email", metavar="EMAIL", help="your account e-mail (asked for when left out)")
    p.set_defaults(func=cmd_login)

    p = command(
        "logout", "Sign out and remove the saved session from this computer.", EXAMPLES_LOGOUT
    )
    p.set_defaults(func=cmd_logout)

    p = command(
        "whoami", "Show the signed-in account and how long the sign-in is valid.", EXAMPLES_WHOAMI
    )
    p.set_defaults(func=cmd_whoami)


def sign_in_message(prog: str, exc: AccountRequired) -> str:
    """The friendly text printed when a command needs an account (exit code 5)."""
    lines = [f"{exc}"]
    if exc.reason in (
        "no_session",
        "invalid",
        "expired",
        "revoked",
        "offline_expired",
        "unavailable",
    ):
        lines += [
            "",
            "radarx-desktop needs a free account, in the app, in the terminal and in Python.",
            f"  Sign in:        {prog} login",
            '  New here?       open the radarx-desktop app and choose "Create your free account"',
            f"  Check status:   {prog} whoami",
        ]
    if exc.reason == "dependency":
        lines += ["", "  pip install -U PyJWT cryptography"]
    return "\n".join(lines)


NO_TERMINAL = (
    "cannot sign in here: there is no terminal to type the password into (the input is\n"
    "redirected, or this program was started without a console).\n"
    "  Run 'rxd login' in a normal terminal window (over ssh: 'ssh -t HOST'), or sign in once in\n"
    "  the radarx-desktop app; the command line shares that sign-in.\n"
    "A password is never taken from the command line, a file or an environment variable."
)


def _no_terminal(out: Any) -> int:
    """Exit code 5 (still not signed in), like every command that needs an account."""
    print(f"error: {NO_TERMINAL}", file=sys.stderr)
    if out.json:
        out.emit_json({"signed_in": False, "error": "no_terminal"})
    return EXIT_ACCOUNT


def _cancelled(out: Any) -> int:
    print("sign-in cancelled", file=sys.stderr)
    if out.json:
        out.emit_json({"signed_in": False, "error": "cancelled"})
    return EXIT_ACCOUNT


def stdin_is_a_terminal() -> bool:
    """Is a person typing at this program's input?

    ``isatty()`` alone is not enough on Windows: it is also true for the ``NUL`` device (input
    redirected from nothing), and the hidden prompt then waits for ever. A real console answers
    ``GetConsoleMode``.
    """
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):  # no stdin, or a closed one
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_uint32())))
    except (AttributeError, OSError):
        return True


def cmd_login(args: argparse.Namespace, out: Any) -> int:
    # The password is typed at a terminal, never taken from anywhere else; without one there is
    # nothing to ask (a redirected input, an IDE or agent shell, a scheduled job).
    if not stdin_is_a_terminal():
        return _no_terminal(out)
    email = (args.email or "").strip()
    try:
        if not email:
            email = input("E-mail: ").strip()
        if not email:
            raise cli_usage("an e-mail is needed")
        password = getpass.getpass("Password (not shown): ")
    except EOFError:  # end of input at a prompt (Ctrl-D, Ctrl-Z)
        return _cancelled(out)
    if not password:
        raise cli_usage("a password is needed")
    try:
        account = core.login_with_password(email, password)
    except AccountServerError as exc:
        print(f"error: {gotrue.friendly_login_error(exc)}", file=sys.stderr)
        return 1
    except (
        AccountRequired
    ) as exc:  # a token that does not verify, or an account that is not allowed
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""
    activity.record("sign_in", surface="cli")
    if out.json:
        out.emit_json({"signed_in": True, "email": account.email})
    else:
        out.result(f"Signed in as {account.email}.")
    return 0


def cli_usage(message: str) -> Exception:
    from engine.cli import UsageError

    return UsageError(message)


def cmd_logout(args: argparse.Namespace, out: Any) -> int:
    removed = core.logout()
    if out.json:
        out.emit_json({"signed_out": removed})
    else:
        out.result("Signed out." if removed else "You were not signed in.")
    return 0


def cmd_whoami(args: argparse.Namespace, out: Any) -> int:
    try:
        account = core.require_account()
    except AccountRequired as exc:
        if out.json:
            out.emit_json({"signed_in": False, "reason": exc.reason})
        else:
            print(sign_in_message(_prog(), exc), file=sys.stderr)
        return _exit_account()
    if account.bypassed:
        text = "The account check is switched off (RADARX_AUTH_DISABLED=1, a development setting)."
        if out.json:
            out.emit_json({"signed_in": False, "bypass": True})
        else:
            out.result(text)
        return 0
    when = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(account.expires_at))
        if account.expires_at
        else "?"
    )
    info = {
        "signed_in": True,
        "email": account.email,
        "session": account.source,
        "token_valid_until": account.expires_at,
        "offline_grace_days_left": account.grace_days_left,
    }
    if out.json:
        out.emit_json(info)
        return 0
    out.result(f"Signed in as {account.email}")
    if account.stale:
        out.result(
            f"  offline: the server has not been reached for a while; this sign-in still works for "
            f"{account.grace_days_left} more day(s) (limit {config.OFFLINE_GRACE_DAYS} days)"
        )
    else:
        renewal = (
            "renewed automatically" if account.source == "cli" else "renewed while the app is open"
        )
        out.result(f"  sign-in valid until {when} ({renewal})")
    return 0


def _prog() -> str:
    from engine import cli

    return cli.PROG


def _exit_account() -> int:
    from engine import cli

    return cli.EXIT_ACCOUNT
