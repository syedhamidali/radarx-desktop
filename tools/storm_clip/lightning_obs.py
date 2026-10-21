"""Observed lightning for the storm clip: readers of local lightning files.

The code lives in the engine (``python/engine/lightning_obs.py``) because ``rxd animate`` uses it too;
this module keeps the names the offline clip tool and its tests import.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from engine.lightning_obs import (  # noqa: E402, F401
    ObservedFlash,
    _relative,
    _to_utc,
    flashes_from_glm_dataset,
    flashes_from_lma_dataset,
    read_flashes,
    to_bundle,
)
