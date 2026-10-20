"""``python -m xrd ...``: the deprecated former name of ``python -m rxd`` (the ``rxd`` command)."""

from __future__ import annotations

from engine.cli import main_xrd

if __name__ == "__main__":
    raise SystemExit(main_xrd())
