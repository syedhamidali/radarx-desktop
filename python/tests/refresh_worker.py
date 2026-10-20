"""Child process of the renewal stress tests (tests/test_account_refresh_stress.py).

Starts ``--threads`` threads, waits until the wall-clock time ``--go-at`` so that every thread of
every process begins together, and each thread runs the gate's session check once. Prints one JSON
line: how many checks passed and the reasons of the ones that did not. Environment (set by the
test): RADARX_CONFIG_DIR, RADARX_SUPABASE_URL (a local mock), RADARX_SUPABASE_KEY, PYTHONPATH.

``--broken-flock`` makes ``flock`` do nothing, like a network file system without a lock daemon.
``--no-refresh-lock`` removes the renewal lock altogether (the control of the stress test).
``--file-keychain FILE`` stores the session in a fake keychain shared through that file.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--go-at", type=float, required=True)
    ap.add_argument("--broken-flock", action="store_true")
    ap.add_argument("--no-refresh-lock", action="store_true", help="test control: no lock at all")
    ap.add_argument("--file-keychain", help="use a fake keychain kept in this file (shared)")
    ap.add_argument("--checks", type=int, default=1, help="checks per thread")
    args = ap.parse_args()

    from engine.account import core, store, tokens

    if args.file_keychain:
        from engine.account import vault
        from tests.keychain_helpers import FileKeychain

        fake = FileKeychain(Path(args.file_keychain))
        vault.backend_factory = lambda: fake
        vault.reset()
    if args.broken_flock:
        store._FileLock._try_lock = lambda self: None  # type: ignore[method-assign]
    if args.no_refresh_lock:
        import contextlib

        @contextlib.contextmanager
        def no_lock(self, timeout=None):  # the control: what the code was before the lock
            yield

        store.SessionStore.refresh_locked = no_lock  # type: ignore[method-assign]
    tokens.current_jwks()  # imports done before the starting gun
    results: list[str] = []
    guard = threading.Lock()

    def work() -> None:
        while time.time() < args.go_at:
            time.sleep(0.001)
        for _ in range(args.checks):
            try:
                core.check_stored_session()
                outcome = "ok"
            except core.AccountRequired as exc:
                outcome = f"refused:{exc.reason}"
            except Exception as exc:  # reported, never hidden
                outcome = f"error:{type(exc).__name__}"
            with guard:
                results.append(outcome)

    threads = [threading.Thread(target=work) for _ in range(args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps({"results": results}))


if __name__ == "__main__":
    main()
