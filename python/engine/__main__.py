"""``python -m engine ...`` is the same as the ``xrd`` command (see :mod:`engine.cli`)."""

from __future__ import annotations

from engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
