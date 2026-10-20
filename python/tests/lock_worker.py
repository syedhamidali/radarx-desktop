"""Child process of the stale-lock herd test (tests/test_account_review2.py): enters the renewal
lock with ``flock`` disabled (like a network file system without a lock daemon), checks that nobody
else is inside at the same time, and reports one word."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> None:
    directory, go_at, hold = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    from engine.account import store

    store._FileLock.__enter__ = lambda self: self  # type: ignore[method-assign]
    store._FileLock.__exit__ = lambda self, *a: None  # type: ignore[method-assign]
    while time.time() < go_at:
        time.sleep(0.001)
    try:
        with store._RefreshLock(directory, timeout=20.0):
            marker = directory / "INSIDE"
            try:
                os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                alone = True
            except FileExistsError:
                alone = False
            time.sleep(hold)
            if alone:
                os.unlink(marker)
            print("ok" if alone else "VIOLATION")
    except TimeoutError:
        print("timeout")


if __name__ == "__main__":
    main()
