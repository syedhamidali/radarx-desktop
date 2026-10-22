"""``rxd serve``: the engine for the app, run on a cluster or server (docs/cluster.md).

Registered by :func:`engine.cli.build_parser`; the work is in :mod:`engine.serve`. Standard library
only at import, like the rest of the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import Any

EPILOG = (
    "examples:\n"
    "  rxd serve                              # a free port, a new token, 2 hours idle limit\n"
    "  rxd serve --port 8765 --idle-minutes 30\n"
    "  rxd serve --token-file ~/.radarx-token # batch job: the token goes to a private file\n"
    "\n"
    "It prints the address, a token (shown once) and the ssh line to run on your own computer:\n"
    "  ssh -L PORT:localhost:PORT user@cluster\n"
    "then enter the address and the token in the app (Settings > Engine). The engine listens on\n"
    "this computer only (127.0.0.1); the token is new for every run; one app can be connected at\n"
    "a time. Do not run heavy work on a login node: start this inside a batch job instead.\n"
    "Needs a signed-in account on this computer (rxd login), like every command."
)


def add_parser(command: Callable[..., argparse.ArgumentParser]) -> argparse.ArgumentParser:
    p = command(
        "serve",
        "Run the engine here so the app on your own computer can use this machine's data (ssh).",
        EPILOG,
    )
    p.add_argument(
        "--port",
        type=int,
        metavar="N",
        help="port to listen on (1024-65535; default: a free one is chosen and printed)",
    )
    p.add_argument(
        "--host",
        metavar="ADDR",
        default="127.0.0.1",
        help="interface to listen on: 127.0.0.1 or localhost, nothing else is accepted",
    )
    p.add_argument(
        "--idle-minutes",
        type=float,
        default=None,
        metavar="MIN",
        help="stop after this many minutes without a request from the app (default 120; 0 = never)",
    )
    p.add_argument(
        "--token-file",
        metavar="FILE",
        help="write the token to this file (readable only by you) instead of printing it; "
        "for batch jobs whose output is stored. Removed when the engine stops",
    )
    p.set_defaults(func=cmd_serve)
    return p


def cmd_serve(args: argparse.Namespace, out: Any) -> int:
    from engine import cli, serve

    try:
        idle = serve.DEFAULT_IDLE_MINUTES if args.idle_minutes is None else args.idle_minutes
        if idle < 0:
            raise serve.ServeError("--idle-minutes must be 0 (never) or more")
        token_file = None
        if args.token_file:
            from pathlib import Path

            token_file = Path(args.token_file).expanduser()
        config = serve.ServeConfig(
            host=args.host,
            port=serve.check_port(args.port),
            idle_minutes=idle,
            token_file=token_file,
        )
        engine = serve.RemoteEngine(config)
    except serve.ServeError as exc:
        raise cli.UsageError(str(exc)) from None

    if token_file is not None:
        try:
            serve.write_token_file(token_file, engine.token)
        except serve.ServeError as exc:
            raise cli.UsageError(str(exc)) from None
        except OSError as exc:
            print(
                f"{cli._err_label(sys.stderr)} cannot write {token_file}: {exc.strerror}",
                file=sys.stderr,
            )
            return cli.EXIT_FAILED

    if out.verbose:  # -v: who connected and left; -vv: everything (the token is never logged)
        import logging

        logging.disable(logging.NOTSET)
        logging.getLogger().setLevel(logging.DEBUG if out.verbose >= 2 else logging.INFO)

    def announce(running: serve.RemoteEngine) -> None:
        # the one place the token is shown (and only when it did not go to a file)
        print(serve.start_banner(running, show_token=token_file is None), flush=True)

    try:
        asyncio.run(engine.run(announce))
    except OSError as exc:
        print(
            f"{cli._err_label(sys.stderr)} cannot listen on {engine.host}:{config.port}: "
            f"{exc.strerror or exc}. Choose another --port, or leave it out.",
            file=sys.stderr,
        )
        return cli.EXIT_FAILED
    except KeyboardInterrupt:  # Windows has no signal handler in the event loop
        print("stopped", file=sys.stderr)
    finally:
        serve.remove_quietly(token_file)
    if engine.stop_reason:
        print(f"engine stopped: {engine.stop_reason}", file=sys.stderr)
    return cli.EXIT_OK
