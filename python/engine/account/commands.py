"""The ``rxd login``, ``rxd logout`` and ``rxd whoami`` commands (registered by :mod:`engine.cli`).

Ways to sign in: the password typed at a hidden prompt (:func:`getpass.getpass`), ONE line of
standard input (``--password-stdin``), or a social provider in the browser (``--browser``). The
password is never on the command line or in the environment and is never stored: only the session
the server returns is kept, in the shared per-user session file.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from typing import Any

from engine.account import activity, config, core, gotrue, oauth_login, pat
from engine.account.errors import AccountRequired, AccountServerError
from engine.account.store import SessionStore

EXAMPLES_LOGIN = (
    "examples:\n"
    "  rxd login\n"
    "  rxd login --email you@example.org\n"
    "  rxd login --email you@example.org --password-stdin < password.txt\n"
    "  rxd login --browser --provider google\n"
    "  rxd login --token < token.txt\n"
    "\n"
    "Signs in with your radarx account (the same account as in the app; create it in the app or\n"
    "at the sign-up screen). The password is asked for without echo, or read from ONE line of\n"
    "standard input with --password-stdin (never from the command line or the environment), and\n"
    "is never stored. Accounts made with Google, Apple or Facebook have no password: use\n"
    "--browser (needs a browser on this same computer). For batch jobs, cron, CI and clusters\n"
    "use an access token (created in the app: Account > Access tokens): --token reads it from\n"
    "standard input and stores it, or set RADARX_TOKEN for a job that should leave nothing\n"
    "behind. The session is kept in a private file in your user configuration folder and shared\n"
    "with Python (import rxd). 'rxd whoami' shows it and 'rxd logout' removes it."
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
    p.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from ONE line of standard input (needs --email; for scripts)",
    )
    p.add_argument(
        "--browser",
        action="store_true",
        help="sign in with Google, Apple or Facebook in your browser (on this same computer)",
    )
    p.add_argument(
        "--provider",
        choices=oauth_login.PROVIDERS,
        help="with --browser: which provider (asked for in a terminal when left out)",
    )
    p.add_argument(
        "--token",
        action="store_true",
        help="sign in with a personal access token read from standard input (for automation)",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="with --browser: only print the address, do not open a browser window",
    )
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
    lines = [f"{exc.headline if exc.app_owned and exc.headline else exc}"]
    if exc.reason == "expired" and exc.app_owned:
        lines += [
            "",
            "  Either:  open the radarx-desktop app (it signs in again by itself), then retry",
            f"  or:      {prog} login   (a sign-in of its own for the terminal)",
            f"  Status:  {prog} whoami",
        ]
    elif exc.reason in (
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


#: The longest password line accepted on standard input (the server limit is far lower).
MAX_PASSWORD_CHARS = 1024


def read_password_line(stream: Any) -> str:
    """ONE line from ``stream`` (a trailing newline is removed, nothing else is trimmed).

    Reads at most ``MAX_PASSWORD_CHARS`` plus the line ending: a longer line, an empty one, or
    bytes that are not text are refused, and none of them is ever echoed in a message.
    """
    try:
        line = stream.readline(MAX_PASSWORD_CHARS + 3)
    except (UnicodeError, ValueError, OSError):
        raise cli_usage("standard input did not contain a readable password line") from None
    if not isinstance(line, str):
        line = bytes(line).decode("utf-8", "strict") if line else ""
    if line.endswith("\n"):
        line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
    if len(line) > MAX_PASSWORD_CHARS:
        raise cli_usage(f"the password line is longer than {MAX_PASSWORD_CHARS} characters")
    if not line:
        raise cli_usage("no password was given on standard input (an empty line)")
    return line


def cmd_login(args: argparse.Namespace, out: Any) -> int:
    if args.token:
        return _login_token(args, out)
    if args.browser:
        return _login_browser(args, out)
    if args.provider or args.no_open:
        raise cli_usage("--provider and --no-open go with --browser")
    email = (args.email or "").strip()
    if args.password_stdin:
        if not email:
            raise cli_usage("--password-stdin needs --email")
        if sys.stdin.isatty():
            raise cli_usage(
                "--password-stdin reads a pipe or a file, not a keyboard (it would be shown as "
                "you type). Run 'rxd login' without it to type the password hidden."
            )
        password = read_password_line(sys.stdin)
    else:
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
    return _signed_in(account, out)


def storage_lines() -> list[str]:
    """Where the sign-in is kept, in plain words (never silent about the file fallback)."""
    where, backend, note = core.storage_description()
    if where == "keychain":
        return [f"  kept in the {backend or 'system keychain'}; the session file holds no token"]
    if where == "memory":
        return [f"  kept in memory only ({pat.ENV_VARIABLE}); nothing is written to disk"]
    if where == "file":
        private = (
            "a private file (readable only by you)"
            if os.name != "nt"
            else "a file in your profile folder"
        )
        return [f"  kept in {private}" + (f"; the keychain is not used: {note}" if note else "")]
    return []


def _signed_in(account: core.Account, out: Any) -> int:
    activity.record("sign_in", surface="cli")
    if out.json:
        out.emit_json({"signed_in": True, "email": account.email})
    else:
        out.result(f"Signed in as {account.email}.")
        for line in storage_lines():
            out.result(line)
    return 0


def _login_token(args: argparse.Namespace, out: Any) -> int:
    if args.email or args.password_stdin or args.browser or args.provider or args.no_open:
        raise cli_usage("--token does not go with --email, --password-stdin or --browser")
    if sys.stdin.isatty():
        token = getpass.getpass("Access token (not shown): ").strip()
    else:
        token = read_password_line(sys.stdin).strip()
    if not pat.valid_format(token):
        raise cli_usage(
            "that is not a radarx access token (it starts with rxd_pat_ and is 51 characters). "
            "Create one in the app: Account > Access tokens."
        )
    try:
        account = core.login_with_token(token)
    except AccountServerError as exc:
        print(f"error: {pat.friendly_error(exc)}", file=sys.stderr)
        return 1
    except AccountRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        token = ""
    activity.record("sign_in", surface="cli")
    if out.json:
        out.emit_json(
            {
                "signed_in": True,
                "email": account.email,
                "session": "pat",
                "token_name": account.token_name,
                "token_expires_at": account.token_expires_at,
                **_storage_json(),
            }
        )
    else:
        out.result(f"Signed in as {account.email} with the access token '{account.token_name}'.")
        out.result(f"  it expires on {_date(account.token_expires_at)}; rxd logout removes it here")
        for line in storage_lines():
            out.result(line)
        out.result("  (revoke it for good in the app: Account > Access tokens)")
    return 0


def _date(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch)) if epoch else "?"


def _choose_provider(args: argparse.Namespace) -> str:
    if args.provider:
        return str(args.provider)
    if not sys.stdin.isatty():
        raise cli_usage("--browser needs --provider google|apple|facebook")
    names = oauth_login.PROVIDERS
    for i, name in enumerate(names, 1):
        print(f"  {i}. {oauth_login.PROVIDER_NAMES[name]}", file=sys.stderr)
    choice = input("Sign in with (number or name): ").strip().lower()
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return names[int(choice) - 1]
    if choice in names:
        return choice
    raise cli_usage("choose google, apple or facebook")


def _login_browser(args: argparse.Namespace, out: Any) -> int:
    if args.email or args.password_stdin:
        raise cli_usage("--browser does not take --email or --password-stdin")
    provider = _choose_provider(args)

    def say(text: str) -> None:
        print(text, file=sys.stderr)  # progress goes to stderr; stdout stays the result

    try:
        account = oauth_login.login(provider, say=say, open_browser=not args.no_open)
    except oauth_login.OAuthFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except AccountRequired as exc:  # a token that does not verify, or an account not allowed
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return _signed_in(account, out)


def cli_usage(message: str) -> Exception:
    from engine.cli import UsageError

    return UsageError(message)


def cmd_logout(args: argparse.Namespace, out: Any) -> int:
    was_token = (stored := SessionStore().load()) is not None and stored.source == "pat"
    try:
        removed = core.logout()
    except OSError as exc:  # the keychain would not give the entry up: nothing was removed
        print(
            f"error: the sign-in could not be removed from the system keychain ({exc}). "
            "It is still there; unlock the keychain and run the command again.",
            file=sys.stderr,
        )
        return 1
    env = pat.from_environment() is not None
    if out.json:
        out.emit_json({"signed_out": removed, "environment_token": env})
    else:
        out.result("Signed out." if removed else "You were not signed in.")
        if was_token:
            out.result(
                "  the access token was removed from this computer only; to cancel it, revoke it "
                "in the app (Account > Access tokens)"
            )
        if env:
            out.result(
                f"  note: {pat.ENV_VARIABLE} is set in this environment and still signs you in; "
                "unset it"
            )
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
        "renewed_by": "app" if account.source == "app" else "rxd",
        **(
            {
                "token_name": account.token_name,
                "token_expires_at": account.token_expires_at,
                "token_from": "RADARX_TOKEN" if account.origin == "env" else "session file",
            }
            if account.is_token
            else {}
        ),
        "token_valid_until": account.expires_at,
        "offline_grace_days_left": account.grace_days_left,
        **_storage_json(),
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
        out.result(f"  sign-in valid until {when}")
    for line in _renewal_lines(account, _prog()):
        out.result(line)
    for line in storage_lines():
        out.result(line)
    return 0


def _storage_json() -> dict[str, str]:
    where, backend, note = core.storage_description()
    return {"storage": where, "storage_backend": backend, "storage_note": note}


def _renewal_lines(account: core.Account, prog: str) -> list[str]:
    """Which program keeps this sign-in alive, and what to do when it is not running."""
    if account.is_token:
        where = (
            f"{pat.ENV_VARIABLE} (environment, kept in memory only)"
            if account.origin == "env"
            else "the private session file (rxd login --token)"
        )
        return [
            f"  access token '{account.token_name}', expires {_date(account.token_expires_at)}; "
            f"from {where}",
            f"  {prog} and Python exchange it for a one-hour sign-in automatically; revoke it "
            "in the app (Account > Access tokens)",
        ]
    if account.source == "app":
        lines = [
            "  made by the radarx-desktop app, which renews it while it is open; "
            f"{prog} and Python do not"
        ]
        if account.stale:
            lines.append(
                f"  when you are online again: open the app, or run '{prog} login' "
                "for a sign-in of its own"
            )
        return lines
    return [f"  made by '{prog} login'; {prog} and Python renew it (the app does not)"]


def _prog() -> str:
    from engine import cli

    return cli.PROG


def _exit_account() -> int:
    from engine import cli

    return cli.EXIT_ACCOUNT
