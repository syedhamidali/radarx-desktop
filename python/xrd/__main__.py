"""``python -m xrd ...`` is the same as the ``xrd`` command."""

from __future__ import annotations

from engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
