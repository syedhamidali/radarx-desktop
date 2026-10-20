"""``python -m rxd ...`` is the same as the ``rxd`` command."""

from __future__ import annotations

from engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
