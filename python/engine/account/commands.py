"""The ``rxd login``, ``rxd logout`` and ``rxd whoami`` commands (registered by :mod:`engine.cli`).

The password is read with :func:`getpass.getpass` (never echoed, never on the command line, never
stored: only the session the server returns is kept, in the shared per-user session file).
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Any

from engine.account import activity, config, core, gotrue
from engine.account.errors import AccountRequired, AccountServerError

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


def cmd_login(args: argparse.Namespace, out: Any) -> int:
    email = (args.email or "").strip()
    if not email:
        if not sys.stdin.isatty():
            raise cli_usage("give your e-mail with --email (and type the password when asked)")
        email = input("E-mail: ").strip()
    if not email:
        raise cli_usage("an e-mail is needed")
    password = getpass.getpass("Password (not shown): ")
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
        out.result(f"  sign-in valid until {when} (renewed automatically)")
    return 0


def _prog() -> str:
    from engine import cli

    return cli.PROG


def _exit_account() -> int:
    from engine import cli

    return cli.EXIT_ACCOUNT
