"""The tile proxy now lives in the engine (``engine/animate_tiles.py``, shared with ``rxd animate``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from engine.animate_tiles import *  # noqa: E402,F401,F403
from engine.animate_tiles import (  # noqa: E402,F401
    MAX_ZOOM,
    MIN_INTERVAL_S,
    SOURCES,
    USER_AGENT,
    TileCache,
    default_cache_dir,
)
